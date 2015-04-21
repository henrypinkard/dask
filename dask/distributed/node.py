from __future__ import print_function

from zmqompute import ComputeNode
from threading import Thread, Lock
from multiprocessing.pool import ThreadPool
from contextlib import contextmanager
import uuid
import random
import multiprocessing
import zmq
import dask
from toolz import partial, get, curry
from time import time
import sys
try:
    from cPickle import dumps, loads, HIGHEST_PROTOCOL
except ImportError:
    from pickle import dumps, loads, HIGHEST_PROTOCOL


DEBUG = True

context = zmq.Context()

with open('log', 'w') as f:  # delete file
    pass

def log(*args):
    with open('log', 'a') as f:
        print(*args, file=f)


@contextmanager
def logerrors():
    try:
        yield
    except Exception as e:
        log('Error!', str(e))
        raise

class Worker(object):
    """ Asynchronous worker in a distributed dask computation pool


    See Also
    --------

    """
    def __init__(self, scheduler, data, nthreads=100,
                 dumps=partial(dumps, protocol=HIGHEST_PROTOCOL),
                 loads=loads, address=None, port=None):
        self.data = data
        self.pool = ThreadPool(nthreads)
        self.dumps = dumps
        self.loads = loads
        self.address = address
        self.scheduler = scheduler
        self.status = 'run'
        if address is None:
            if port is None:
                port = 6464
            address = 'tcp://%s:%d' % (socket.gethostname(), port)
        self.address = address

        self.dealer = context.socket(zmq.DEALER)
        self.dealer.setsockopt(zmq.IDENTITY, address)
        self.dealer.connect(scheduler)
        self.dealer.send_multipart(['', b'Register'])

        self.router = context.socket(zmq.ROUTER)
        self.router.bind(self.address)

        log(self.address, 'Start up', self.scheduler)

        self.lock = Lock()

        self.functions = {'status': status,
                          'collect': self.collect,
                          'compute': self.compute,
                          'getitem': self.data.__getitem__,
                          'setitem': self.data.__setitem__,
                          'delitem': self.data.__delitem__}

        self._listen_scheduler_thread = Thread(target=self.listen_to_scheduler)
        self._listen_scheduler_thread.start()
        self._listen_workers_thread = Thread(target=self.listen_to_workers)
        self._listen_workers_thread.start()

    def execute_and_reply(self, func, args, kwargs, jobid, send=None):
        """ Execute function. Reply with header and result.

        Computes func(*args, **kwargs) then sends the result along the given
        send function.

        This is intended to be run asynchronously in a separate thread.

        See also:
            send_to_scheduler
            send_to_worker
            listen_to_scheduler
            listen_to_workers
        """
        try:
            function = self.functions[func]
            result = function(*args, **kwargs)
            status = 'OK'
        except KeyError as e:
            result = e
            if func not in self.functions:
                status = 'Function %s not found' % func
            else:
                status = 'Error'
        except Exception as e:
            result = e
            status = 'Error'

        header = {'jobid': jobid,
                  'status': status}

        log(self.address, 'Finish computation', header, send)

        with logerrors():
            if send is not None:
                send(header, result)

    def send_to_scheduler(self, header, payload):
        log(self.address, 'Send to scheduler', header)
        header['address'] = self.address
        with self.lock:
            self.dealer.send_multipart([self.dumps(header),
                                        self.dumps(payload)])

    @curry
    def send_to_worker(self, address, header, result):
        log(self.address, 'Send to worker', address, header)
        header['address'] = self.address
        with self.lock:
            self.router.send_multipart([address,
                                        self.dumps(header),
                                        self.dumps(result)])

    def unpack_function(self, header, payload):
        """ Deserialize and unpack payload with sane defaults """
        payload = self.loads(payload)
        header = self.loads(header)

        log(self.address, "Receive payload", payload)

        jobid = header.get('jobid', None)
        reply = header.get('reply', True)

        func = payload['function']
        args = payload.get('args', ())
        if not isinstance(args, tuple):
            args = (args,)
        kwargs = payload.get('kwargs', dict())

        return func, args, kwargs, jobid, reply

    def listen_to_scheduler(self):
        """
        Event loop listening to commands from scheduler

        Payload should deserialize into a dict of the following form:

            {'function': name of function to call, see self.functions,
             'jobid': job identifier, defaults to None,
             'args': arguments to pass to function, defaults to (),
             'kwargs': keyword argument dict, defauls to {},
             'reply': whether or not a reply is desired}

        So the minimal request would be as follows:

        >>> sock = context.socket(zmq.DEALER)  # doctest: +SKIP
        >>> sock.connect('tcp://my-address')   # doctest: +SKIP

        >>> sock.send(dumps({'function': 'status'}))  # doctest: +SKIP

        Or a more complex packet might be as follows:

        >>> sock.send(dumps({'function': 'setitem',
        ...                  'args': ('x', 10),
        ...                  'jobid': 123}))  # doctest: +SKIP

        We match the function string against ``self.functions`` to pull out the
        actual function.  We then execute this function with the provided
        arguments in another thread from ``self.pool`` using
        ``self.execute_and_reply``.  This sends results back to the sender.

        See Also:
            listen_to_workers
            execute_and_reply
        """
        while self.status != 'closed':
            # Wait on request
            if not self.dealer.poll(100):
                continue
            header, payload = self.dealer.recv_multipart()

            func, args, kwargs, jobid, reply = self.unpack_function(header, payload)
            log(self.address, 'Receive job from scheduler', jobid, func)

            # Execute job in separate thread
            future = self.pool.apply_async(self.execute_and_reply,
                          args=(func, args, kwargs, jobid,
                                self.send_to_scheduler if reply else None))

    def listen_to_workers(self):
        while self.status != 'closed':
            # Wait on request
            if not self.router.poll(100):
                continue
            address, header, payload = self.router.recv_multipart()

            func, args, kwargs, jobid, reply = self.unpack_function(header, payload)
            header = self.loads(header)
            log(self.address, 'Receive job from worker', header['address'], jobid, func)

            self.pool.apply_async(self.execute_and_reply,
                    args=(func, args, kwargs, jobid,
                          self.send_to_worker(address) if reply else None))

    def collect(self, locations):
        """ Collect data from peers

        Given a dictionary of desired data and who holds that data

        >>> locations = {'x': ['tcp://alice:5000', 'tcp://bob:5000'],
        ...              'y': ['tcp://bob:5000']}

        This fires off getitem reqeusts to one of the hosts for each piece of
        data then blocks on all of the responses, then inserts this data into
        ``self.data``.
        """
        socks = []

        log(self.address, 'Collect data from peers', locations)
        # Send out requests for data
        for key, locs in locations.items():
            if key in self.data:  # already have this locally
                continue
            sock = context.socket(zmq.DEALER)
            sock.connect(random.choice(locs))  # randomly select one peer
            header = {'address': self.address, 'jobid': key}
            payload = {'function': 'getitem',
                       'args': (key,)}
            sock.send_multipart([self.dumps(header),
                                 self.dumps(payload)])
            socks.append(sock)

        log(self.address, 'Waiting on data replies')
        # Wait on replies.  Store results in self.data.
        for sock in socks:
            header, payload = sock.recv_multipart()
            header = self.loads(header)
            payload = self.loads(payload)
            log(self.address, 'Receive data', header['address'],
                                              header['jobid'])
            self.data[header['jobid']] = payload

    def compute(self, key, task, locations):
        """ Compute dask task

        Given a key, task, and locations of data

            key -- 'z'
            task -- (add, 'x', 'y')
            locations -- {'x': ['tcp://alice:5000']}

        Collect necessary data from locations, merge into self.data (see
        ``collect``), then compute task and store into ``self.data``.
        """
        self.collect(locations)

        start = time()
        status = "OK"
        log(self.address, "Start computation", key, task)
        try:
            result = dask.core.get(self.data, task)
            end = time()
        except Exception as e:
            status = e
            end = time()
        else:
            self.data[key] = result
        log(self.address, "End computation", key, task, status)

        return {'key': key,
                'duration': end - start,
                'status': status}

    def close(self):
        if self.pool._state == multiprocessing.pool.RUN:
            log(self.address, 'Close')
            self.status = 'closed'
            self.pool.close()
            self.pool.join()

    def __del__(self):
        self.close()


def status():
    return 'OK'
