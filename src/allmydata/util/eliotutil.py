"""
Tools aimed at the interaction between Tahoe-LAFS implementation and Eliot.
"""

from __future__ import (
    unicode_literals,
    print_function,
    division,
    absolute_import,
)

__all__ = [
    "eliot_friendly_generator_function",
    "inline_callbacks",
    "validateInstanceOf",
    "validateSetMembership",
    "RELPATH",
    "VERSION",
    "LAST_UPLOADED_URI",
    "LAST_DOWNLOADED_URI",
    "LAST_DOWNLOADED_TIMESTAMP",
    "PATHINFO",
]

from sys import exc_info
from functools import wraps
from contextlib import contextmanager
from weakref import WeakKeyDictionary


from eliot import (
    Message,
)

from eliot import (
    Field,
)
from eliot._validation import (
    ValidationError,
)

from twisted.internet.defer import (
    inlineCallbacks,
)

from .fileutil import (
    PathInfo,
)

class _GeneratorContext(object):
    def __init__(self, execution_context):
        self._execution_context = execution_context
        self._contexts = WeakKeyDictionary()
        self._current_generator = None

    def init_stack(self, generator):
        stack = list(self._execution_context._get_stack())
        self._contexts[generator] = stack

    def get_stack(self):
        if self._current_generator is None:
            # If there is no currently active generator then we have no
            # special stack to supply.  Let the execution context figure out a
            # different answer on its own.
            return None
        # Otherwise, give back the action context stack we've been tracking
        # for the currently active generator.  It must have been previously
        # initialized (it's too late to do it now)!
        return self._contexts[self._current_generator]

    @contextmanager
    def context(self, generator):
        previous_generator = self._current_generator
        try:
            self._current_generator = generator
            yield
        finally:
            self._current_generator = previous_generator


from eliot._action import _context
_the_generator_context = _GeneratorContext(_context)


def use_generator_context():
    _context.get_sub_context = _the_generator_context.get_stack
use_generator_context()


def eliot_friendly_generator_function(original):
    """
    Decorate a generator function so that the Eliot action context is
    preserved across ``yield`` expressions.
    """
    @wraps(original)
    def wrapper(*a, **kw):
        # Keep track of whether the next value to deliver to the generator is
        # a non-exception or an exception.
        ok = True

        # Keep track of the next value to deliver to the generator.
        value_in = None

        # Create the generator with a call to the generator function.  This
        # happens with whatever Eliot action context happens to be active,
        # which is fine and correct and also irrelevant because no code in the
        # generator function can run until we call send or throw on it.
        gen = original(*a, **kw)

        # Initialize the per-generator Eliot action context stack to the
        # current action stack.  This might be the main stack or, if another
        # decorated generator is running, it might be the stack for that
        # generator.  Not our business.
        _the_generator_context.init_stack(gen)
        while True:
            try:
                # Whichever way we invoke the generator, we will do it
                # with the Eliot action context stack we've saved for it.
                # Then the context manager will re-save it and restore the
                # "outside" stack for us.
                with _the_generator_context.context(gen):
                    if ok:
                        value_out = gen.send(value_in)
                    else:
                        value_out = gen.throw(*value_in)
                    # We have obtained a value from the generator.  In
                    # giving it to us, it has given up control.  Note this
                    # fact here.  Importantly, this is within the
                    # generator's action context so that we get a good
                    # indication of where the yield occurred.
                    #
                    # This might be too noisy, consider dropping it or
                    # making it optional.
                    Message.log(message_type=u"yielded")
            except StopIteration:
                # When the generator raises this, it is signaling
                # completion.  Leave the loop.
                break
            else:
                try:
                    # Pass the generator's result along to whoever is
                    # driving.  Capture the result as the next value to
                    # send inward.
                    value_in = yield value_out
                except:
                    # Or capture the exception if that's the flavor of the
                    # next value.
                    ok = False
                    value_in = exc_info()
                else:
                    ok = True

    return wrapper


def inline_callbacks(original):
    """
    Decorate a function like ``inlineCallbacks`` would but in a more
    Eliot-friendly way.  Use it just like ``inlineCallbacks`` but where you
    want Eliot action contexts to Do The Right Thing inside the decorated
    function.
    """
    return inlineCallbacks(
        eliot_friendly_generator_function(original)
    )

def validateInstanceOf(t):
    """
    Return an Eliot validator that requires values to be instances of ``t``.
    """
    def validator(v):
        if not isinstance(v, t):
            raise ValidationError("{} not an instance of {}".format(v, t))
    return validator

def validateSetMembership(s):
    """
    Return an Eliot validator that requires values to be elements of ``s``.
    """
    def validator(v):
        if v not in s:
            raise ValidationError("{} not in {}".format(v, s))
    return validator

RELPATH = Field.for_types(
    u"relpath",
    [unicode],
    u"The relative path of a file in a magic-folder.",
)

VERSION = Field.for_types(
    u"version",
    [int, long],
    u"The version of the file.",
)

LAST_UPLOADED_URI = Field.for_types(
    u"last_uploaded_uri",
    [unicode, bytes, None],
    u"The filecap to which this version of this file was uploaded.",
)

LAST_DOWNLOADED_URI = Field.for_types(
    u"last_downloaded_uri",
    [unicode, bytes, None],
    u"The filecap from which the previous version of this file was downloaded.",
)

LAST_DOWNLOADED_TIMESTAMP = Field.for_types(
    u"last_downloaded_timestamp",
    [float, int, long],
    u"(XXX probably not really, don't trust this) The timestamp of the last download of this file.",
)

PATHINFO = Field(
    u"pathinfo",
    lambda v: None if v is None else {
        "isdir": v.isdir,
        "isfile": v.isfile,
        "islink": v.islink,
        "exists": v.exists,
        "size": v.size,
        "mtime_ns": v.mtime_ns,
        "ctime_ns": v.ctime_ns,
    },
    u"The metadata for this version of this file.",
    validateInstanceOf((type(None), PathInfo)),
)
