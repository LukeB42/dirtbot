#!/usr/bin/env python
"""
Implements a scriptable single-threaded multi-server IRC bot.
Can load configuration values from remote hosts via HTTP.
Uses only the standard library. Probably compiles with cx_freeze et al.

Psybernetics 2015.
"""
import os
import sys
import pwd
import copy
import json
import time
import fcntl
import select
import socket
import signal
import urllib
import logging
import hashlib
import datetime

try:
    import setproctitle
    setproctitle.setproctitle("dirtbot")
except ImportError:
    pass

class DirtBot(object):
    """
    Maintains server connections.
    """
    def __init__(self, config):
        self.config      = config
        self.scripts     = Scripts(config)
        self.connections = {}
        if not 'linux' in sys.platform:
            self.poll    = select.poll()
        else:
            self.poll    = select.epoll()

        if "servers" in config:
            for s in config['servers']:
                self.connect(s)
    
    def connect(self, server):
        """
        Create a new Connection instance for a server object as defined in our
        config.
        """
        if not 'host' in server: return
        c = Connection(self, server)
        self.connections[c.fileno] = c
        self.poll.register(c.socket)
        c.connect()

    def run(self):
        while 1:
            time.sleep(0.5)

            # Event scheduler goes here.

            for fd, event in self.poll.poll():

                connection = self.connections[fd]

                try:
                    data   = connection.socket.recv(1024)
                except socket.error:
                    continue

                if not data: continue
                elif len(data) > 0:
                    buf  = ""
                    buf += str(data)
                    while buf.find("\n") != -1:
                        line, buf = buf.split("\n", 1)
                        line = line.rstrip()
                        connection.read(line)

    def halt(self):
        for c in self.connections.values():
            self.config.log("Disconnecting from %s." % c.host)
            c.cmd("quit", "Leaving")
            c.socket.close()

class Connection(object):
    """
    Maintains a single server connection and executes scripts.
    """
    def __init__(self, bot, server):
        self.modes     = []
        self.bot       = bot
        self.host      = server['host']
        self.port      = int(server['port']) if 'port' in server else 6667
        self.nick      = bot.config['nick']
        self.realname  = bot.config['realname']
        self.dchannels = server['channels'] if 'channels' in server else []
        self.channels  = {}
        self.users     = {}
        self.socket    = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    def connect(self):
        try:
            self.socket.connect((self.host, self.port))
            fcntl.fcntl(self.socket, fcntl.F_SETFL, os.O_NONBLOCK)
        except Exception, e:
            self.bot.config.log(e.message, "error")
            return
        self.cmd('nick', self.nick)
        self.cmd('user', "%s %s * :%s" % (self.nick, self.nick, self.realname))

    def msg(self, target, msg):
        self.cmd('privmsg', target + ' :' + msg)
    
    def cmd(self, command, args=[], prefix=None):
        if prefix:
            self.socket.send('%s %s\n' % (prefix + command.upper(), args))
        else:
            self.socket.send('%s %s\n' % (command.upper(), args))

    def read(self, line):
        """
        Take an unparsed IRC line and determine how to respond.
        """
        try: line = Line(line)
        except Exception, e:
            self.bot.config.log(e.message, "error")
            return
        self.bot.config.log(line, self.host)
        # Execute scripts
        env = {
                'connection': self,
                'line': copy.deepcopy(line)
              }
        for script in self.bot.scripts.values():
            try: script.execute(env)
            except Exception, e:
                self.bot.config.log(e.message, "error")

        # Do maintenence stuff like responding to PING
        function = getattr(self, "handle_%s" % line.command, None)
        if not function: return
        function(line)

        # TODO: check line.nick and line.channel and append to the appropriate
        #       buffers.

    def handle_001(self, line):
        """
        Join default channels when welcomed.
        """
        for channel in self.dchannels:
            self.cmd("join", channel)

    def handle_ping(self, line):
        self.cmd("pong")

    @property
    def fileno(self):
        return self.socket.fileno()

class Channel(object):
    def __init__(self, connection, name):
        self.connection = connection
        self.name       = name
        self.topic      = ""
        self.nicks      = set()
        self.modes      = []
        self.buffer     = []

class User(object):
    def __init__(self, connection, nick):
        self.connection = connection
        self.nick       = nick
        self.channels   = {}
        self.realname   = ''
        self.host       = ''
        self.modes      = []
        self.buffer     = []

class Line(object):
    "A line from IRC"
    def __init__(self, line):
        self.line = line
        self.time = time.time()
        self.command = ''
        self.host    = ''
        self.args    = []
        self.nick    = ''
        self.ident   = ''
        self.channel = ''
        # parse for attributes
        host = ''
        trailing = []
        if not line:
            raise IRCError('Received an empty line.')
        if line[0] == ':':
            self.host, line = line[1:].split(' ', 1)
        if line.find(' :') != -1:
            line, trailing = line.split(' :', 1)
            self.args = line.split()
            self.args.append(trailing)
        else:
            self.args = line.split()
        self.command = self.args.pop(0).lower()
        if self.command == "privmsg" and self.args[0].startswith('#'):
            self.channel = self.args.pop(0)
        if '!' in self.host:
            self.nick, self.ident = self.host.split('!')

    def __repr__(self):
        if self.line:
            t = datetime.datetime.fromtimestamp(int(self.time)).strftime('%H:%M:%S')
            if self.nick and self.channel:
                return "%s <%s/%s> %s" % (t, self.channel, self.nick, ''.join(self.args))
            return "%s %s" % (t, self.line)
        return "<Line>"

class Scripts(dict):
    """
    A container for a suite of executable code objects.
    """
    def __init__(self, config, *args, **kwargs):
        dict.__init__(self, *args, **kwargs)
        self.dir     = None
        self.config  = config

        dir = os.path.abspath(config['scripts'])
        if not os.path.isdir(config['scripts']):
            config.log("%s isn't a valid system path." % dir, "error")
            return

        self.dir = dir

    def reload(self, *args): # args caught for SIGHUP handler
        if self.dir:
            if len(self):
                self.config.log("Reloading scripts.")
            for file in os.listdir(self.dir):
                self.unload(file)
                self.load(file)

    def load(self, file):
        # File is a python keyword that was going unused.
        file = os.path.abspath(os.path.join(self.dir, file))

        for script in self.values():
            if script.file == file: return

        if os.path.isfile(file):
            self[file] = Script(file)
            if 'debug' in self.config:
                self[file].read_on_exec = self.config['debug']
            self.config.log("Loaded %s" % file)

    def unload(self, file):
        file = os.path.abspath(os.path.join(self.dir, file))

        if file in self:
            del self[file]

class Script(object):
    """
    Represents the execution environment for a third-party script.
    We send custom values into the environment and work with the results.
    Scripts can also call any methods on objects put in their environment.
    """
    def __init__(self, file=None, env={}):
        self.env          = env
        self.file         = file
        self.code         = None
        self.hash         = None
        self.cache        = {}
        self.script       = ''
        self.read_on_exec = None

    def execute(self, env={}):
        if not self.code or self.read_on_exec: self.compile()
        if env: self.env = env
        self.env['cache'] = self.cache
        exec self.code in self.env
        del self.env['__builtins__']
        if 'cache' in self.env.keys():
            self.cache = self.env['cache']
        return (self.env)

    def compile(self, script=''):
        if self.file:
            f = file(self.file, 'r')
            self.script = f.read()
            f.close()
        elif script:
            self.script = script
        if self.script:
            hash = sha1sum(self.script)
            if self.hash != hash:
                self.hash = hash
                self.code = compile(self.script, '<string>', 'exec')
            self.script = ''

    def __getitem__(self, key):
        if key in self.env.keys():
            return (self.env[key])
        else:
            raise (KeyError(key))

    def keys(self):
        return self.env.keys()

class Config(dict):
    def __init__(self, logger, file_path, *args, **kwargs):
        dict.__init__(self, *args, **kwargs) 
        self.log       = logger
        self.file_path = file_path
        self.parsed    = False
        try:
            self.update(self.load())
        except Exception, e:
            self.log(e)
            raise SystemExit
        self.parse()

    def load(self):
        if self.file_path.startswith('http') and "://" in self.file_path:
            try:
                config = urllib.urlopen(self.file_path)
            except Exception, e:
                raise
        else:
            try:
                fd     = open(self.file_path, "r")
                config = fd.read()
                fd.close()
            except Exception, e:
                raise
        try:
            config = json.loads(config)
        except Exception, e:
            raise

        return config

    def parse(self):
        
        # External files to read and compile
        if not 'scripts' in self:
            self['scripts'] = ''
            self.log("No scripts directory defined.")
            self.log("Won't be doing much once connected.")
        
        # Change UID
        if 'run_as' in self and self['run_as'] != os.getlogin():
            try:
                uid = pwd.getpwnam(self['run_as'])[2]
                os.setuid(uid)
                self.log("Now running as %s." % options.run_as)
            except:
                self.log("Couldn't switch user to \"%s\"." % options.run_as, "error")
                raise SystemExit

        # Detach from TTY
        if 'daemonise' in self and self['daemonise']:
            Daemonise()

        # Max length of Channel.buffer and User.buffer
        if not 'bufferlen' in self:
            self['bufferlen'] = 512
        self.parsed = True

def Daemonise():
    pidfile = __file__.replace('.','').replace(os.path.sep,'') + '.pid'
    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0) # End parent
    except OSError, e:
        sys.stderr.write("fork #1 failed: %d (%s)\n" % (e.errno, e.strerror))
        sys.exit(-2)
    os.setsid()
    os.umask(0)
    try:
        pid = os.fork()
        if pid > 0:
            try: 
                # TODO: Read the file first and determine if already running.
                f = file(pidfile, 'w')
                f.write(str(pid))
                f.close()
            except IOError, e:
                logging.error(e)
                sys.stderr.write(repr(e))
            sys.exit(0) # End parent
    except OSError, e:
        sys.stderr.write("fork #2 failed: %d (%s)\n" % (e.errno, e.strerror))
        sys.exit(-2)
    for fd in (0, 1, 2):
        try:
            os.close(fd)
        except OSError:
            pass

def sha1sum(data):
    return hashlib.sha1(data).hexdigest()

class IRCError(Exception):
    """
    Generic error class
    """
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return(repr(self.value))


if __name__ == "__main__":
    def logger(message, level="info"):
        if level in ["debug","error","warning","info"]:
            level = level.upper()
        try: print "%s: %s" % (level, message)
        except: pass

    config = Config(logger, sys.argv[1] if len(sys.argv) > 1 else "config.json")

    dirtbot = DirtBot(config)
    dirtbot.scripts.reload()
    signal.signal(signal.SIGHUP, dirtbot.scripts.reload)

    try:
        dirtbot.run()
    except KeyboardInterrupt:
        print
        dirtbot.halt()
