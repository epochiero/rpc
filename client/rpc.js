(function () {
  /* global angular */
  var module = angular.module('rpc', []);
  module.service('rpc', [
    '$q', function ($q) {

      return {

        connect: function (ws) {
          var
            last_id = 0,
            pending_calls = {},
            pending_messages = [];

          function auto_id() {
            last_id += 1;
            return last_id;
          }

          function RpcCall(scope, service, method, args) {
            var self = this;
            this.deferred = $q.defer();
            this.scope = scope;
            this.service = service;
            this.method = method;
            this.args = args;
            this.finalizer_deferred = null;
            this.handler_promises = [];
            this.finalizer_deferred = $q.defer();
          }

          RpcCall.prototype = {
            send:             function () {
              var id = auto_id(),
                msg = JSON.stringify({
                  id:      id,
                  service: this.service,
                  method:  this.method,
                  args:    this.args
                });
              pending_calls[id] = this;
              if (ws.readyState === 1) {
                ws.send(msg);
              } else {
                pending_messages.push(msg);
              }
            },
            then:             function (callback, errback) {
              var promise = this.deferred.promise.then(callback, errback);
              if (errback) {
                this.handles_error = true;
              }
              this.handler_promises.push(promise);
            },
            into:             function (attr) {
              var self = this;
              this.then(function (value) {
                self.scope[attr] = value;
              });
            },
            finalize:         function (callback, errback) {
              return this.finalizer_deferred.promise.then(callback, errback);
            },
            process_response: function (response) {
              var self = this, fire_finalizers, resolution;
              if (response.success) {
                resolution = response.result;
              } else {
                resolution = $q.reject(response.error);
              }
              self.deferred.resolve(resolution);
              fire_finalizers = function () {
                self.finalizer_deferred.resolve(resolution);
              };
              $q.all(this.handler_promises).then(fire_finalizers,
                fire_finalizers);
            }
          };
          ws.onopen = function () {
            angular.forEach(pending_messages, function (msg) {
              ws.send(msg);
            });
          };
          ws.onmessage = function (ws_msg) {
            var
              rpc_msg = JSON.parse(ws_msg.data),
              rpc_call = pending_calls[rpc_msg.id];
            delete pending_calls[rpc_msg.id];
            rpc_call.scope.$apply(function () {
              rpc_call.process_response(rpc_msg);
            });
          };

          function rpc(svc_dot_method) {
            var scope = this,
              split_svc = svc_dot_method.split('.'),
              svc = split_svc[0],
              method = split_svc[1],
              args = [].splice.call(arguments, 1),
              call = new RpcCall(scope, svc, method, args);
            call.finalize(null, function (error) {
              scope.$emit('rpc', error, call);
            });
            call.send();
            return call;
          }

          return rpc;
        }
      };
    }
  ]);

  // This is nasty, need to factor out the deferred part of an rpc call
  // to reuse finalize and friends
  module.factory('rpc_watch', [
    '$timeout', function (timeout) {
      return function (rpc_attr) {
        return function (svc_dot_method) {
          var scope = this;
          var watch_attrs = [].splice.call(arguments, 1);
          return {
            into: function (dst_attr) {
              var fired_watches = [];
              angular.forEach(watch_attrs, function (attr) {
                scope.$watch(attr, function (value) {
                  if (!angular.isDefined(value)) {
                    return;
                  }
                  fired_watches.push(attr);
                  timeout(function () {
                    var args = [svc_dot_method];
                    if (fired_watches.length > 0) {
                      angular.forEach(watch_attrs, function (attr) {
                        args.push(scope[attr]);
                      });
                      scope[rpc_attr].apply(scope, args).then(function (value) {
                          scope[dst_attr] = value;
                        }
                      );
                      fired_watches = [];
                    }
                  });
                });
              })
            }
          }
        }
      };
    }
  ]);

}());
