"""Routes HTTP Server."""
#pylint: disable=import-error,invalid-name,broad-except
#pylint: disable=missing-docstring
import sys
import traceback
import urlparse
import cgi
import json
import threading
from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
from SocketServer import ThreadingMixIn

from pyrevit.api import UI
from pyrevit.coreutils.logger import get_logger

from pyrevit.routes import exceptions as excp
from pyrevit.routes import router
from pyrevit.routes import handler


mlogger = get_logger(__name__)


DEFAULT_STATUS = 500
DEFAULT_SOURCE = __name__


# instance of event handler created when this module is loaded
# on hosts main thread. Creating external events on non-main threads
# are prohibited by the host. this event handler is reconfigured
# for every request registered by this module
REQUEST_HNDLR = handler.RequestHandler()
EVENT_HNDLR = UI.ExternalEvent.Create(REQUEST_HNDLR)


class Request(object):
    # TODO: implement headers and other stuff
    def __init__(self, path='/', method='GET', data=None, params=None):
        self.path = path
        self.method = method
        self.data = data
        self._headers = {}
        self._params = params or []

    @property
    def headers(self):
        return self._headers

    @property
    def params(self):
        return self._params

    def add_header(self, key, value):
        self._headers[key] = value


class Response(object):
    # TODO: implement headers and other stuff
    def __init__(self, status=200, data=None):
        self.status = status
        self.data = data

    def get_header(self, key):
        # TODO: implement Response.get_header
        pass


class HttpRequestHandler(BaseHTTPRequestHandler):
    def _parse_api_path(self):
        url_parts = urlparse.urlparse(self.path)
        if url_parts:
            levels = url_parts.path.split('/')
            # host:ip/<api_name>/<route>/.../.../...
            if levels and len(levels) >= 2:
                api_name = levels[1]
                if len(levels) > 2:
                    api_path = '/' + '/'.join(levels[2:])
                else:
                    api_path = '/'
                return api_name, api_path
        return None, None

    def _write_error(self, message, status, source):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(
            {
                "exception": {
                    "source": source,
                    "message": message
                }
            }
        ))

    def _write_exeption(self, excep):
        self._write_error(
            message=str(excep),
            status=excep.status if hasattr(excep, 'status') else DEFAULT_STATUS,
            source=excep.source if hasattr(excep, 'source') else DEFAULT_SOURCE,
        )

    def _parse_request_info(self):
        # find the app
        api_name, api_path = self._parse_api_path() #type:str, str
        if not api_name:
            raise excp.APINotDefinedException(api_name)
        return api_name, api_path

    def _find_route_handler(self, api_name, path, method):
        route, route_handler = router.get_route_handler(
            api_name=api_name,
            path=path,
            method=method
            )
        if not route_handler:
            raise excp.RouteHandlerNotDefinedException(api_name, path, method)
        return route, route_handler

    def _prepare_request(self, route, path, method):
        # process request data
        data = None
        content_length = self.headers.getheader('content-length') # type: str
        if content_length and content_length.isnumeric():
            data = self.rfile.read(int(content_length))
            # format data
            content_type_header = self.headers.getheader('content-type')
            if content_type_header:
                content_type, _ = cgi.parse_header(content_type_header)
                if content_type == 'application/json':
                    data = json.loads(data)

        return Request(
            path=path,
            method=method,
            data=data,
            params=router.extract_route_params(route.pattern, path)
        )

    def _prepare_host_handler(self, request, route_handler):
        # create the base Revit external event handler
        # upon Raise(), finds and runs the appropriate func
        REQUEST_HNDLR.request = request
        REQUEST_HNDLR.handler = route_handler
        return REQUEST_HNDLR, EVENT_HNDLR

    def _call_host_event_sync(self, req_hndlr, event_hndlr):
        # reset handler
        req_hndlr.reset()
        # raise request to host
        extevent_raise_response = event_hndlr.Raise()
        if extevent_raise_response == UI.ExternalEventRequest.Denied:
            raise excp.RouteHandlerDeniedException(req_hndlr.request)
        elif extevent_raise_response == UI.ExternalEventRequest.TimedOut:
            raise excp.RouteHandlerTimedOutException(req_hndlr.request)

        # wait until event has been picked up by host for execution
        while event_hndlr.IsPending:
            pass

        # wait until handler signals completion
        req_hndlr.join()

    def _parse_reponse(self, req_hndlr):
        # grab response from req_hndlr.response
        # req_hndlr.response getter is thread-safe
        response = req_hndlr.response

        # now process reponse based on obj type
        # it is an exception is has .message
        # write the exeption to output and return
        if hasattr(response, 'message'):
            self._write_exeption(response)
            return

        # plain text response
        if isinstance(response, str):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(response)

        # any obj that has .status and .data, OR
        # any json serializable object
        # serialize before sending results
        # in case exceptions happen in serialization,
        # there are no double status in response header
        else:
            status = 200
            data_string = None
            # can not directly check for isinstance(x, Response)
            # this module is executed on a different Engine than the
            # script that registered the request handler function, thus
            # the Response in script engine does not match Response
            # registered when this module was loaded
            if hasattr(response, 'data'):
                data_string = json.dumps(getattr(response, 'data'))
            else:
                data_string = json.dumps(response)

            if hasattr(response, 'status'):
                status = getattr(response, 'status')

            self.send_response(status)
            if data_string:
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(data_string)

    def _handle_route(self, method):
        # process the given url and find API and route
        api_name, path = self._parse_request_info()

        # find the handler function registered by the API and route
        route, route_handler = self._find_route_handler(api_name, path, method)

        # prepare a request obj to be passed to registered handler
        request = self._prepare_request(route, path, method)

        # create a handler and event object in host
        req_hndlr, event_hndlr = \
            self._prepare_host_handler(request, route_handler)

        # do the handling work
        self._call_host_event_sync(req_hndlr, event_hndlr)

        # prepare response
        self._parse_reponse(req_hndlr)

    def _process_request(self, method):
        # this method is wrapping the actual handler and is
        # catching all the excp
        try:
            self._handle_route(method=method)
        except Exception as ex:
            # get exception info
            sys.exc_type, sys.exc_value, sys.exc_traceback = \
                sys.exc_info()
            # go back one frame to grab exception stack from handler
            # and grab traceback lines
            tb_report = ''.join(
                traceback.format_tb(sys.exc_traceback)[1:]
            )
            self._write_exeption(
                excp.ServerException(
                    message=str(ex),
                    exception_type=sys.exc_type,
                    exception_traceback=tb_report
                )
            )

    # CRUD Methods ------------------------------------------------------------
    # create
    def do_POST(self):
        self._process_request(method='POST')

    # read
    def do_GET(self):
        self._process_request(method='GET')

    # update
    def do_PUT(self):
        self._process_request(method='PUT')

    # delete
    def do_DELETE(self):
        self._process_request(method='DELETE')

    # rest of standard http methods -------------------------------------------
    # https://developer.mozilla.org/en-US/docs/Web/HTTP/Methods
    def do_HEAD(self):
        self._process_request(method='HEAD')

    def do_CONNECT(self):
        self._process_request(method='CONNECT')

    def do_OPTIONS(self):
        self._process_request(method='OPTIONS')

    def do_TRACE(self):
        self._process_request(method='TRACE')

    def do_PATCH(self):
        self._process_request(method='PATCH')


class ThreadedHttpServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True

    def shutdown(self):
        self.socket.close()
        HTTPServer.shutdown(self)


class RoutesServer(object):
    def __init__(self, host, port):
        self.server = ThreadedHttpServer((host, port), HttpRequestHandler)
        self.host = host
        self.port = port
        self.start()

    def __str__(self):
        return "Routes server is listening on http://%s:%s" \
            % (self.host or "0.0.0.0", self.port)

    def __repr__(self):
        return '<RoutesServer @ http://%s:%s>' \
            % (self.host or "0.0.0.0", self.port)

    def start(self):
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.daemon = True
        self.server_thread.start()

    def waitForThread(self):
        self.server_thread.join()

    def stop(self):
        self.server.shutdown()
        self.waitForThread()
