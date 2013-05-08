import json
import linecache
import os
import ast
from traceback import extract_tb
from geventwebsocket import WebSocketError
import sys
import webob, webob.dec, webob.static, webob.exc
import gevent
from gevent import pywsgi
from geventwebsocket.handler import WebSocketHandler
from json_encoder import RegistryJsonEncoder
import db


class ErrorHandler(object):
  def __init__(self, root, user_filenames):
    self.filename_mapping = {}
    for user_filename in user_filenames:
      if user_filename.endswith('.pyc'):
        user_filename = user_filename[:-1]

      if user_filename.startswith(root):
        mapped_filename = user_filename[len(root):]
      else:
        mapped_filename = user_filename

      self.filename_mapping[user_filename] = mapped_filename

  def format_trace(self, traceback):
    formatted = []
    for filename, line_number, function_name, code in extract_tb(traceback):
      if filename in self.filename_mapping:
        formatted.append(dict(filename=self.filename_mapping[filename],
                              line=line_number, function=function_name,
                              code=code))
    return formatted

  def get_error_response(self):
    exception_type, value, traceback = sys.exc_info()
    return dict(success=False,
                error=dict(type=exception_type.__name__,
                           message=value.message,
                           traceback=self.format_trace(traceback))
    )

class MultilineErrorHandler(ErrorHandler):

  def format_trace(self, traceback):
    formatted = []
    for filename, line_number, function_name, code in self.extract_tb(traceback):
      if filename in self.filename_mapping:
        formatted.append(dict(filename=self.filename_mapping[filename],
                              line=line_number, function=function_name,
                              code=code))
    return formatted

  def extract_tb(self, tb, limit=None):
      if limit is None:
          if hasattr(sys, 'tracebacklimit'):
              limit = sys.tracebacklimit
      tb_list = []
      n = 0
      while tb is not None and (limit is None or n < limit):
          f = tb.tb_frame
          lineno = tb.tb_lineno
          co = f.f_code
          filename = co.co_filename
          name = co.co_name
          statement = linecache.getline(filename, lineno)
          orig_lineno, full_stm = self.parse_statement(statement, filename, lineno)
          tb_list.insert(0, (filename, orig_lineno, name, full_stm))
          tb = tb.tb_next
          n = n+1
      return tb_list

  #Recursively parse lines until we get a parseable statement
  def parse_statement(self, statement, filename, lineno):
    try:
      curr_statement = statement.strip()
      ast.parse(curr_statement)
    except Exception:
      lineno -= 1
      prev_line = linecache.getline(filename, lineno)
      try:
        curr_statement = (prev_line + statement).strip()
        ast.parse(curr_statement)
      except Exception:
        lineno += 2
        next_line = linecache.getline(filename, lineno)
        try:
          curr_statement = (statement + next_line).strip()
          ast.parse(curr_statement)
        except Exception:
          try:
            curr_statement = (prev_line + statement + next_line).strip()
            lineno -= 2
            ast.parse(curr_statement)
          except Exception:
            self.parse_statement(curr_statement, filename, lineno)
    return lineno, curr_statement


class JsonRpcServer(object):
  last_resort_response = {'success': False, 'error': {'type': 'internal'}}

  def __init__(self, services, encoder, decoder, error_handler):
    self.services = services
    self.encoder = encoder
    self.decoder = decoder
    self.error_handler = error_handler

  def server_loop(self, websocket):
    while True:
      try:
        message = websocket.receive()
        if message is None:
          break
        gevent.spawn(self.handle_message, websocket, message)
      except WebSocketError:
        break

  def handle_message(self, websocket, message):
    call_spec = self.decoder.decode(message)
    call_id = call_spec.get('id')
    if call_id is None:
      return
    response = self.last_resort_response
    try:
      result = self.handle_rpc(call_id, call_spec)
      response = dict(success=True, result=result)
    except:
      response = self.error_handler.get_error_response()
    finally:
      response['id'] = call_id
      encoded = self.encoder.encode(response)
      websocket.send(encoded)


  def handle_rpc(self, call_id, call_spec):
    service = call_spec.get('service', '')
    method = call_spec.get('method', '')
    args = call_spec.get('args', [])
    kwargs = call_spec.get('kwargs', {})
    service_instance = self.services.get(service, None)
    if service_instance is None:
      return dict(success=False, id=call_id,
                  error=dict(message="invalid service name"))
    method_instance = getattr(service_instance, method, None)
    if method_instance is None or not callable(method_instance):
      return dict(success=False, id=call_id,
                  error=dict(message="invalid method name"))
    return method_instance(*args, **kwargs)


class WebServer(object):
  def __init__(self, rpc_server):
    self.static_files = webob.static.DirectoryApp('client')
    self.rpc_server = rpc_server

  @webob.dec.wsgify
  def handler(self, request):
    """
    :type request: webob.Request
    """
    root = request.path_info_peek().lstrip('/')
    if root == 'client':
      request.path_info_pop()
      return self.static_files(request)
    elif root == 'rpc':
      websocket = request.environ['wsgi.websocket']
      self.rpc_server.server_loop(websocket)
    elif root == '':
      return webob.exc.HTTPMovedPermanently(location='/client')


#gevent / geventwebsocket logging error fix
def log_request(self):
  log = self.server.log
  if log:
    if hasattr(log, "info"):
      log.info(self.format_request() + '\n')
    else:
      log.write(self.format_request() + '\n')


gevent.pywsgi.WSGIHandler.log_request = log_request

handler = None


def setup_modules(module_names):
  services = {}
  filenames = []
  for module_name in module_names:
    module = __import__(module_name)
    filenames.append(module.__file__)
    mod_services = module.setup_services()
    services.update(mod_services)
  return services, filenames


def main(run_server, module_names):
  global handler
  services, filenames = setup_modules(module_names)
  encoder = RegistryJsonEncoder(sort_keys=True, indent=2)
  error_handler = MultilineErrorHandler(os.path.dirname(__file__) + '/', filenames)
  decoder = json.JSONDecoder()
  rpc_server = JsonRpcServer(services, encoder, decoder, error_handler)
  web_server = WebServer(rpc_server)
  handler = web_server.handler
  if run_server:
    server = pywsgi.WSGIServer(("", 8100), handler,
                               handler_class=WebSocketHandler)
    server.serve_forever()


if __name__ == '__main__':
  main(run_server=True, module_names=['services'])

