# coding=utf-8
# Copyright 2008, Sean B. Palmer, inamidst.com
# Copyright © 2012, Elad Alfassa <elad@fedoraproject.org>
# Copyright 2012-2015, Elsie Powell, http://embolalia.com
#
# Licensed under the Eiffel Forum License 2.

from __future__ import unicode_literals, absolute_import, print_function, division

from ast import literal_eval
import collections
import os
import re
import sys
import threading
import time

from sopel import tools
from sopel import irc
from sopel.db import SopelDB
from sopel.tools import stderr, Identifier
import sopel.tools.jobs
from sopel.trigger import PreTrigger, Trigger
from sopel.module import NOLIMIT
from sopel.logger import get_logger
import sopel.loader

LOGGER = get_logger(__name__)

if sys.version_info.major >= 3:
    unicode = str
    basestring = str
    py3 = True
else:
    py3 = False


class _CapReq(object):
    def __init__(self, prefix, module, failure=None, arg=None, success=None):
        def nop(bot, cap):
            pass
        # TODO at some point, reorder those args to be sane
        self.prefix = prefix
        self.module = module
        self.arg = arg
        self.failure = failure or nop
        self.success = success or nop


class Sopel(irc.Bot):
    def __init__(self, config, daemon=False):
        irc.Bot.__init__(self, config)
        self._daemon = daemon  # Used for iPython. TODO something saner here
        # `re.compile('.*') is re.compile('.*')` because of caching, so we need
        # to associate a list with each regex, since they are unexpectedly
        # indistinct.
        self._callables = {
            'high': collections.defaultdict(list),
            'medium': collections.defaultdict(list),
            'low': collections.defaultdict(list)
        }

        self.config = config
        """The :class:`sopel.config.Config` for the current Sopel instance."""

        self.doc = {}
        """
        A dictionary of command names to their docstring and example, if
        declared. The first item in a callable's commands list is used as the
        key in version *3.2* onward. Prior to *3.2*, the name of the function
        as declared in the source code was used.
        """

        self._command_groups = collections.defaultdict(list)
        """A mapping of module names to a list of commands in it."""

        self.stats = {}  # deprecated, remove in 7.0
        self._times = {}
        """
        A dictionary mapping lower-case'd nicks to dictionaries which map
        funtion names to the time which they were last used by that nick.
        """

        self.server_capabilities = {}
        """A dict mapping supported IRCv3 capabilities to their options.

        For example, if the server specifies the capability ``sasl=EXTERNAL``,
        it will be here as ``{"sasl": "EXTERNAL"}``. Capabilities specified
        without any options will have ``None`` as the value.

        For servers that do not support IRCv3, this will be an empty set."""

        self.enabled_capabilities = set()
        """A set containing the IRCv3 capabilities that the bot has enabled."""

        self._cap_reqs = dict()
        """A dictionary of capability names to a list of requests"""

        self.privileges = dict()
        """A dictionary of channels to their users and privilege levels

        The value associated with each channel is a dictionary of
        :class:`sopel.tools.Identifier`\\s to
        a bitwise integer value, determined by combining the appropriate
        constants from :mod:`sopel.module`.

        .. deprecated:: 6.2.0
            Use :attr:`channels` instead.
        """

        self.channels = tools.SopelMemory()  # name to chan obj
        """A map of the channels that Sopel is in.

        The keys are Identifiers of the channel names, and map to
        :class:`sopel.tools.target.Channel` objects which contain the users in
        the channel and their permissions.
        """

        self.users = tools.SopelMemory()  # name to user obj
        """A map of the users that Sopel is aware of.

        The keys are Identifiers of the nicknames, and map to
        :class:`sopel.tools.target.User` instances. In order for Sopel to be
        aware of a user, it must be in at least one channel which they are also
        in.
        """

        self.db = SopelDB(config)
        """The bot's database, as a :class:`sopel.db.SopelDB` instance."""

        self.memory = tools.SopelMemory()
        """
        A thread-safe dict for storage of runtime data to be shared between
        modules. See :class:`sopel.tools.Sopel.SopelMemory`
        """

        self.shutdown_methods = []
        """List of methods to call on shutdown"""

        self.scheduler = sopel.tools.jobs.JobScheduler(self)
        self.scheduler.start()

        # Set up block lists
        # Default to empty
        if not self.config.core.nick_blocks:
            self.config.core.nick_blocks = []
        if not self.config.core.host_blocks:
            self.config.core.host_blocks = []
        self.setup()

    @property
    def hostmask(self):
        """The current hostmask for the bot :class:`sopel.tools.target.User`

        Bot must be connected and in at least one channel.

        :return: the bot's current hostmask
        :rtype: str
        """
        if not self.users or not self.users.contains(self.nick):
            raise KeyError("'hostmask' not available: bot must be connected and in at least one channel.")

        return self.users.get(self.nick).hostmask

    # Backwards-compatibility aliases to attributes made private in 6.2. Remove
    # these in 7.0
    times = property(lambda self: getattr(self, '_times'))
    command_groups = property(lambda self: getattr(self, '_command_groups'))

    def write(self, args, text=None):  # Shim this in here for autodocs
        """Send a command to the server.

        ``args`` is an iterable of strings, which are joined by spaces.
        ``text`` is treated as though it were the final item in ``args``, but
        is preceeded by a ``:``. This is a special case which  means that
        ``text``, unlike the items in ``args`` may contain spaces (though this
        constraint is not checked by ``write``).

        In other words, both ``sopel.write(('PRIVMSG',), 'Hello, world!')``
        and ``sopel.write(('PRIVMSG', ':Hello, world!'))`` will send
        ``PRIVMSG :Hello, world!`` to the server.

        Newlines and carriage returns ('\\n' and '\\r') are removed before
        sending. Additionally, if the message (after joining) is longer than
        than 510 characters, any remaining characters will not be sent.

        :param collections.Iterable args: an iterable of strings, which will be joined by spaces
        :param str text: a string that will be prepended with a ``:`` and added to the end of the command
        """
        irc.Bot.write(self, args, text=text)

    def setup(self):
        """Set up the Sopel instance."""

        stderr("\nWelcome to Sopel. Loading modules...\n\n")

        modules = sopel.loader.enumerate_modules(self.config)

        error_count = 0
        success_count = 0
        for name in modules:
            path, type_ = modules[name]

            try:
                module, _ = sopel.loader.load_module(name, path, type_)
            except Exception as e:
                error_count = error_count + 1
                filename, lineno = tools.get_raising_file_and_line()
                rel_path = os.path.relpath(filename, os.path.dirname(__file__))
                raising_stmt = "%s:%d" % (rel_path, lineno)
                stderr("Error loading %s: %s (%s)" % (name, e, raising_stmt))
            else:
                try:
                    if hasattr(module, 'setup'):
                        module.setup(self)
                    relevant_parts = sopel.loader.clean_module(
                        module, self.config)
                except Exception as e:
                    error_count = error_count + 1
                    filename, lineno = tools.get_raising_file_and_line()
                    rel_path = os.path.relpath(
                        filename, os.path.dirname(__file__)
                    )
                    raising_stmt = "%s:%d" % (rel_path, lineno)
                    stderr("Error in %s setup procedure: %s (%s)"
                           % (name, e, raising_stmt))
                else:
                    self.register(*relevant_parts)
                    success_count += 1

        if len(modules) > 1:  # coretasks is counted
            stderr('\n\nRegistered %d modules,' % (success_count - 1))
            stderr('%d modules failed to load\n\n' % error_count)
        else:
            stderr("Warning: Couldn't load any modules")

    def unregister(self, obj):
        """Unregister a callable.

        :param object obj: the callable to unregister
        """
        if not callable(obj):
            return
        if hasattr(obj, 'rule'):  # commands and intents have it added
            for rule in obj.rule:
                callb_list = self._callables[obj.priority][rule]
                if obj in callb_list:
                    callb_list.remove(obj)
        if hasattr(obj, 'interval'):
            # TODO this should somehow find the right job to remove, rather than
            # clearing the entire queue. Issue #831
            self.scheduler.clear_jobs()
        if (getattr(obj, '__name__', None) == 'shutdown' and
                    obj in self.shutdown_methods):
            self.shutdown_methods.remove(obj)

    def register(self, callables, jobs, shutdowns, urls):
        """Register a callable.

        :param collections.Iterable callables: an iterable of callables to register
        :param collections.Iterable jobs: an iterable of functions to periodically invoke
        :param collections.Iterable shutdowns: an iterable of functions to call on shutdown
        :param collections.Iterable urls: an iterable of functions to call when matched against a URL
        """

        # Append module's shutdown function to the bot's list of functions to
        # call on shutdown
        self.shutdown_methods += shutdowns
        for callbl in callables:
            if hasattr(callbl, 'rule'):
                for rule in callbl.rule:
                    self._callables[callbl.priority][rule].append(callbl)
            else:
                self._callables[callbl.priority][re.compile('.*')].append(callbl)
            if hasattr(callbl, 'commands'):
                module_name = callbl.__module__.rsplit('.', 1)[-1]
                # TODO doc and make decorator for this. Not sure if this is how
                # it should work yet, so not making it public for 6.0.
                category = getattr(callbl, 'category', module_name)
                self._command_groups[category].append(callbl.commands[0])
            for command, docs in callbl._docs.items():
                self.doc[command] = docs
        for func in jobs:
            for interval in func.interval:
                job = sopel.tools.jobs.Job(interval, func)
                self.scheduler.add_job(job)

        for func in urls:
            self.register_url_callback(func.url_regex, func)

    def part(self, channel, msg=None):
        """Part a channel.

        :param str channel: the channel to leave
        :param str msg: the message to display when leaving a channel
        """
        self.write(['PART', channel], msg)

    def join(self, channel, password=None):
        """Join a channel

        If `channel` contains a space, and no `password` is given, the space is
        assumed to split the argument into the channel to join and its
        password.  `channel` should not contain a space if `password` is given.

        :param str channel: the channel to join
        :param str password: an optional channel password
        """
        if password is None:
            self.write(('JOIN', channel))
        else:
            self.write(['JOIN', channel, password])

    def msg(self, recipient, text, max_messages=1):
        # Deprecated, but way too much of a pain to remove.
        self.say(text, recipient, max_messages)

    def say(self, text, recipient, max_messages=1):
        """Send ``text`` as a PRIVMSG to ``recipient``.

        In the context of a triggered callable, the ``recipient`` defaults to
        the channel (or nickname, if a private message) from which the message
        was received.

        By default, this will attempt to send the entire ``text`` in one
        message. If the text is too long for the server, it may be truncated.
        If ``max_messages`` is given, the ``text`` will be split into at most
        that many messages, each no more than 400 bytes. The split is made at
        the last space character before the 400th byte, or at the 400th byte if
        no such space exists. If the ``text`` is too long to fit into the
        specified number of messages using the above splitting, the final
        message will contain the entire remainder, which may be truncated by
        the server.

        :param str text: the text to send
        :param str recipient: the message recipient
        :param int max_messages: the maximum number of messages to break the text into, defaults to 1
        """
        excess = ''
        if not isinstance(text, unicode):
            # Make sure we are dealing with unicode string
            text = text.decode('utf-8')

        if max_messages > 1:
            # Manage multi-line only when needed
            text, excess = tools.get_sendable_message(text)

        try:
            self.sending.acquire()

            # No messages within the last 3 seconds? Go ahead!
            # Otherwise, wait so it's been at least 0.8 seconds + penalty

            recipient_id = Identifier(recipient)

            if recipient_id not in self.stack:
                self.stack[recipient_id] = []
            elif self.stack[recipient_id]:
                elapsed = time.time() - self.stack[recipient_id][-1][0]
                if elapsed < 3:
                    penalty = float(max(0, len(text) - 40)) / 70
                    wait = min(0.8 + penalty, 2)  # Never wait more than 2 seconds
                    if elapsed < wait:
                        time.sleep(wait - elapsed)

                # Loop detection
                messages = [m[1] for m in self.stack[recipient_id][-8:]]

                # If what we about to send repeated at least 5 times in the
                # last 2 minutes, replace with '...'
                if messages.count(text) >= 5 and elapsed < 120:
                    text = '...'
                    if messages.count('...') >= 3:
                        # If we said '...' 3 times, discard message
                        return

            self.write(('PRIVMSG', recipient), text)
            self.stack[recipient_id].append((time.time(), self.safe(text)))
            self.stack[recipient_id] = self.stack[recipient_id][-10:]
        finally:
            self.sending.release()
        # Now that we've sent the first part, we need to send the rest. Doing
        # this recursively seems easier to me than iteratively
        if excess:
            self.msg(recipient, excess, max_messages - 1)

    def notice(self, text, dest):
        """Send an IRC NOTICE to a user or a channel.

        Within the context of a triggered callable, ``dest`` will default to
        the channel (or nickname, if a private message), in which the trigger
        happened.

        :param str text: the text to send in the NOTICE
        :param str dest: the destination of the NOTICE
        """
        self.write(('NOTICE', dest), text)

    def action(self, text, dest):
        """Send ``text`` as a CTCP ACTION PRIVMSG to ``dest``.

        The same loop detection and length restrictions apply as with
        :func:`say`, though automatic message splitting is not available.

        Within the context of a triggered callable, ``dest`` will default to
        the channel (or nickname, if a private message), in which the trigger
        happened.

        :param str text: the text to send in the CTCP ACTION
        :param str dest: the destination of the CTCP ACTION
        """
        self.say('\001ACTION {}\001'.format(text), dest)

    def reply(self, text, dest, reply_to, notice=False):
        """Prepend ``reply_to`` to ``text``, and send as a PRIVMSG to ``dest``.

        If ``notice`` is ``True``, send a NOTICE rather than a PRIVMSG.

        The same loop detection and length restrictions apply as with
        :func:`say`, though automatic message splitting is not available.

        Within the context of a triggered callable, ``reply_to`` will default to
        the nickname of the user who triggered the call, and ``dest`` to the
        channel (or nickname, if a private message), in which the trigger
        happened.

        :param str text: the text of the reply
        :param str dest: the destination of the reply
        :param str reply_to: the nickname that the reply will be prepended with
        :param bool notice: whether to send the reply as a NOTICE or not, defaults to False
        """
        text = '%s: %s' % (reply_to, text)
        if notice:
            self.notice(text, dest)
        else:
            self.say(text, dest)

    def call(self, func, sopel, trigger):
        """Call a function, after checking whether the function is restricted or subject to limits.

        :param function func: the function to call
        :param SopelWrapper sopel: a SopelWrapper instance
        :param Trigger trigger: the Trigger object for the line from the server that triggered this call
        """
        nick = trigger.nick
        current_time = time.time()
        if nick not in self._times:
            self._times[nick] = dict()
        if self.nick not in self._times:
            self._times[self.nick] = dict()
        if not trigger.is_privmsg and trigger.sender not in self._times:
            self._times[trigger.sender] = dict()

        if not trigger.admin and not func.unblockable:
            if func in self._times[nick]:
                usertimediff = current_time - self._times[nick][func]
                if func.rate > 0 and usertimediff < func.rate:
                    LOGGER.info(
                        "%s prevented from using %s in %s due to user limit: %d < %d",
                        trigger.nick, func.__name__, trigger.sender, usertimediff,
                        func.rate
                    )
                    return
            if func in self._times[self.nick]:
                globaltimediff = current_time - self._times[self.nick][func]
                if func.global_rate > 0 and globaltimediff < func.global_rate:
                    LOGGER.info(
                        "%s prevented from using %s in %s due to global limit: %d < %d",
                        trigger.nick, func.__name__, trigger.sender, globaltimediff,
                        func.global_rate
                    )
                    return

            if not trigger.is_privmsg and func in self._times[trigger.sender]:
                chantimediff = current_time - self._times[trigger.sender][func]
                if func.channel_rate > 0 and chantimediff < func.channel_rate:
                    LOGGER.info(
                        "%s prevented from using %s in %s due to channel limit: %d < %d",
                        trigger.nick, func.__name__, trigger.sender, chantimediff,
                        func.channel_rate
                    )
                    return

        # if channel has its own config section, check for excluded modules/modules methods
        if trigger.sender in self.config:
            channel_config = self.config[trigger.sender]

            # disable listed modules completely on provided channel
            if 'disable_modules' in channel_config:
                disabled_modules = channel_config.disable_modules.split(',')

                # if "*" is used, we are disabling all modules on provided channel
                if '*' in disabled_modules:
                    return
                if func.__module__ in disabled_modules:
                    return

            # disable chosen methods from modules
            if 'disable_commands' in channel_config:
                disabled_commands = literal_eval(channel_config.disable_commands)

                if func.__module__ in disabled_commands:
                    if func.__name__ in disabled_commands[func.__module__]:
                        return

        try:
            exit_code = func(sopel, trigger)
        except Exception:  # TODO: Be specific
            exit_code = None
            self.error(trigger)

        if exit_code != NOLIMIT:
            self._times[nick][func] = current_time
            self._times[self.nick][func] = current_time
            if not trigger.is_privmsg:
                self._times[trigger.sender][func] = current_time

    def dispatch(self, pretrigger):
        """Match an incoming, parsed message from the server and dispatch it to any registered callables.

        :param PreTrigger pretrigger: a parsed message from the server
        """
        args = pretrigger.args
        event, args, text = pretrigger.event, args, args[-1] if args else ''

        if self.config.core.nick_blocks or self.config.core.host_blocks:
            nick_blocked = self._nick_blocked(pretrigger.nick)
            host_blocked = self._host_blocked(pretrigger.host)
        else:
            nick_blocked = host_blocked = None

        list_of_blocked_functions = []
        for priority in ('high', 'medium', 'low'):
            items = self._callables[priority].items()

            for regexp, funcs in items:
                match = regexp.match(text)
                if not match:
                    continue
                user_obj = self.users.get(pretrigger.nick)
                account = user_obj.account if user_obj else None
                trigger = Trigger(self.config, pretrigger, match, account)
                wrapper = SopelWrapper(self, trigger)

                for func in funcs:
                    if (not trigger.admin and
                            not func.unblockable and
                            (nick_blocked or host_blocked)):
                        function_name = "%s.%s" % (
                            func.__module__, func.__name__
                        )
                        list_of_blocked_functions.append(function_name)
                        continue

                    if event not in func.event:
                        continue
                    if hasattr(func, 'intents'):
                        if not trigger.tags.get('intent'):
                            continue
                        match = False
                        for intent in func.intents:
                            if intent.match(trigger.tags.get('intent')):
                                match = True
                        if not match:
                            continue
                    if (trigger.nick.lower() == self.nick.lower() and
                            not func.echo):
                        continue
                    if func.thread:
                        targs = (func, wrapper, trigger)
                        t = threading.Thread(target=self.call, args=targs)
                        t.start()
                    else:
                        self.call(func, wrapper, trigger)

        if list_of_blocked_functions:
            if nick_blocked and host_blocked:
                block_type = 'both'
            elif nick_blocked:
                block_type = 'nick'
            else:
                block_type = 'host'
            LOGGER.info(
                "[%s]%s prevented from using %s.",
                block_type,
                trigger.nick,
                ', '.join(list_of_blocked_functions)
            )

    def _host_blocked(self, host):
        bad_masks = self.config.core.host_blocks
        for bad_mask in bad_masks:
            bad_mask = bad_mask.strip()
            if not bad_mask:
                continue
            if (re.match(bad_mask + '$', host, re.IGNORECASE) or
                    bad_mask == host):
                return True
        return False

    def _nick_blocked(self, nick):
        bad_nicks = self.config.core.nick_blocks
        for bad_nick in bad_nicks:
            bad_nick = bad_nick.strip()
            if not bad_nick:
                continue
            if (re.match(bad_nick + '$', nick, re.IGNORECASE) or
                    Identifier(bad_nick) == nick):
                return True
        return False

    def _shutdown(self):
        stderr(
            'Calling shutdown for %d modules.' % (len(self.shutdown_methods),)
        )

        for shutdown_method in self.shutdown_methods:
            try:
                stderr(
                    "calling %s.%s" % (
                        shutdown_method.__module__, shutdown_method.__name__,
                    )
                )
                shutdown_method(self)
            except Exception as e:
                stderr(
                    "Error calling shutdown method for module %s:%s" % (
                        shutdown_method.__module__, e
                    )
                )
        # Avoid calling shutdown methods if we already have.
        self.shutdown_methods = []

    def cap_req(self, module_name, capability, arg=None, failure_callback=None,
                success_callback=None):
        """Tell Sopel to request a capability when it starts.

        By prefixing the capability with `-`, it will be ensured that the
        capability is not enabled. Similarly, by prefixing the capability with
        `=`, it will be ensured that the capability is enabled. Requiring and
        disabling is "first come, first served"; if one module requires a
        capability, and another prohibits it, this function will raise an
        exception in whichever module loads second. An exception will also be
        raised if the module is being loaded after the bot has already started,
        and the request would change the set of enabled capabilities.

        If the capability is not prefixed, and no other module prohibits it, it
        will be requested.  Otherwise, it will not be requested. Since
        capability requests that are not mandatory may be rejected by the
        server, as well as by other modules, a module which makes such a
        request should account for that possibility.

        The actual capability request to the server is handled after the
        completion of this function. In the event that the server denies a
        request, the `failure_callback` function will be called, if provided.
        The arguments will be a `Sopel` object, and the capability which was
        rejected. This can be used to disable callables which rely on the
        capability. It will be be called either if the server NAKs the request,
        or if the server enabled it and later DELs it.

        The `success_callback` function will be called upon acknowledgement of
        the capability from the server, whether during the initial capability
        negotiation, or later.

        If ``arg`` is given, and does not exactly match what the server
        provides or what other modules have requested for that capability, it is
        considered a conflict.

        :param str module_name: the module requesting the capability
        :param str capability: the capability requested, optionally prefixed with ``+`` or ``=``
        :param str arg: arguments for the capability request
        :param function failure_callback: a function that will be called if the capability request fails
        :param function success_callback: a function that will be called if the capability is successfully requested
        """
        # TODO raise better exceptions
        cap = capability[1:]
        prefix = capability[0]

        entry = self._cap_reqs.get(cap, [])
        if any((ent.arg != arg for ent in entry)):
            raise Exception('Capability conflict')

        if prefix == '-':
            if self.connection_registered and cap in self.enabled_capabilities:
                raise Exception('Can not change capabilities after server '
                                'connection has been completed.')
            if any((ent.prefix != '-' for ent in entry)):
                raise Exception('Capability conflict')
            entry.append(_CapReq(prefix, module_name, failure_callback, arg,
                                 success_callback))
            self._cap_reqs[cap] = entry
        else:
            if prefix != '=':
                cap = capability
                prefix = ''
            if self.connection_registered and (cap not in
                                               self.enabled_capabilities):
                raise Exception('Can not change capabilities after server '
                                'connection has been completed.')
            # Non-mandatory will callback at the same time as if the server
            # rejected it.
            if any((ent.prefix == '-' for ent in entry)) and prefix == '=':
                raise Exception('Capability conflict')
            entry.append(_CapReq(prefix, module_name, failure_callback, arg,
                                 success_callback))
            self._cap_reqs[cap] = entry

    def register_url_callback(self, pattern, callback):
        """Register a ``callback`` for URLs matching the regex ``pattern``

        :param re.Pattern pattern: compiled regex pattern to register
        :param function callback: callable object to handle matching URLs

        .. versionadded:: 7.0

            This method replaces manual management of ``url_callbacks`` in
            Sopel's plugins, so instead of doing this in ``setup()``::

                if not bot.memory.contains('url_callbacks'):
                    bot.memory['url_callbacks'] = tools.SopelMemory()

                regex = re.compile(r'http://example.com/path/.*')
                bot.memory['url_callbacks'][regex] = callback

            use this much more concise pattern::

                regex = re.compile(r'http://example.com/path/.*')
                bot.register_url_callback(regex, callback)

        """
        if not self.memory.contains('url_callbacks'):
            self.memory['url_callbacks'] = tools.SopelMemory()

        if isinstance(pattern, basestring):
            pattern = re.compile(pattern)

        self.memory['url_callbacks'][pattern] = callback

    def unregister_url_callback(self, pattern):
        """Unregister the callback for URLs matching the regex ``pattern``

        :param re.Pattern pattern: compiled regex pattern to unregister callback

        .. versionadded:: 7.0

            This method replaces manual management of ``url_callbacks`` in
            Sopel's plugins, so instead of doing this in ``shutdown()``::

                regex = re.compile(r'http://example.com/path/.*')
                try:
                    del bot.memory['url_callbacks'][regex]
                except KeyError:
                    pass

            use this much more concise pattern::

                regex = re.compile(r'http://example.com/path/.*')
                bot.unregister_url_callback(regex)

        """
        if not self.memory.contains('url_callbacks'):
            # nothing to unregister
            return

        if isinstance(pattern, basestring):
            pattern = re.compile(pattern)

        try:
            del self.memory['url_callbacks'][pattern]
        except KeyError:
            pass

    def search_url_callbacks(self, url):
        """Yield callbacks found for ``url`` matching their regex pattern

        :param str url: URL found in a trigger
        :return: yield 2-value tuples of ``(callback, match)``

        For each pattern that matches the ``url`` parameter, it yields a
        2-value tuple of ``(callable, match)`` for that pattern.

        The ``callable`` is the one registered with
        :meth:`register_url_callback`, and the ``match`` is the result of
        the regex pattern's ``search`` method.

        .. versionadded:: 7.0

        .. seealso::

            The Python documentation for the `re.search`__ function and
            the `match object`__.

        .. __: https://docs.python.org/3.6/library/re.html#re.search
        .. __: https://docs.python.org/3.6/library/re.html#match-objects

        """
        for regex, function in tools.iteritems(self.memory['url_callbacks']):
            match = regex.search(url)
            if match:
                yield function, match


class SopelWrapper(object):
    def __init__(self, sopel, trigger):
        # The custom __setattr__ for this class sets the attribute on the
        # original bot object. We don't want that for these, so we set them
        # with the normal __setattr__.
        object.__setattr__(self, '_bot', sopel)
        object.__setattr__(self, '_trigger', trigger)

    def __dir__(self):
        classattrs = [attr for attr in self.__class__.__dict__
                      if not attr.startswith('__')]
        return list(self.__dict__) + classattrs + dir(self._bot)

    def __getattr__(self, attr):
        return getattr(self._bot, attr)

    def __setattr__(self, attr, value):
        return setattr(self._bot, attr, value)

    def say(self, message, destination=None, max_messages=1):
        if destination is None:
            destination = self._trigger.sender
        self._bot.say(message, destination, max_messages)

    def action(self, message, destination=None):
        if destination is None:
            destination = self._trigger.sender
        self._bot.action(message, destination)

    def notice(self, message, destination=None):
        if destination is None:
            destination = self._trigger.sender
        self._bot.notice(message, destination)

    def reply(self, message, destination=None, reply_to=None, notice=False):
        if destination is None:
            destination = self._trigger.sender
        if reply_to is None:
            reply_to = self._trigger.nick
        self._bot.reply(message, destination, reply_to, notice)
