"""
Ported to Python 3.
"""

from __future__ import annotations

import os
import sys
import shutil
from time import sleep
from os import mkdir, listdir, environ
from os.path import join, exists
from tempfile import mkdtemp, mktemp
from functools import partial
from json import loads

from foolscap.furl import (
    decode_furl,
)

from eliot import (
    to_file,
    log_call,
)

from twisted.python.filepath import FilePath
from twisted.python.procutils import which
from twisted.internet.defer import DeferredList
from twisted.internet.error import (
    ProcessExitedAlready,
    ProcessTerminated,
)

import pytest
import pytest_twisted

from .util import (
    _CollectOutputProtocol,
    _MagicTextProtocol,
    _DumpOutputProtocol,
    _ProcessExitedProtocol,
    _create_node,
    _cleanup_tahoe_process,
    _tahoe_runner_optional_coverage,
    await_client_ready,
    TahoeProcess,
    cli,
    generate_ssh_key,
    block_with_timeout,
)
from allmydata.node import read_config

# No reason for HTTP requests to take longer than four minutes in the
# integration tests. See allmydata/scripts/common_http.py for usage.
os.environ["__TAHOE_CLI_HTTP_TIMEOUT"] = "240"

# Make Foolscap logging go into Twisted logging, so that integration test logs
# include extra information
# (https://github.com/warner/foolscap/blob/latest-release/doc/logging.rst):
os.environ["FLOGTOTWISTED"] = "1"

# pytest customization hooks

def pytest_addoption(parser):
    parser.addoption(
        "--keep-tempdir", action="store_true", dest="keep",
        help="Keep the tmpdir with the client directories (introducer, etc)",
    )
    parser.addoption(
        "--coverage", action="store_true", dest="coverage",
        help="Collect coverage statistics",
    )
    parser.addoption(
        "--force-foolscap", action="store_true", default=False,
        dest="force_foolscap",
        help=("If set, force Foolscap only for the storage protocol. " +
              "Otherwise HTTP will be used.")
    )
    parser.addoption(
        "--runslow", action="store_true", default=False,
        dest="runslow",
        help="If set, run tests marked as slow.",
    )

def pytest_collection_modifyitems(session, config, items):
    if not config.option.runslow:
        # The --runslow option was not given; keep only collected items not
        # marked as slow.
        items[:] = [
            item
            for item
            in items
            if item.get_closest_marker("slow") is None
        ]


@pytest.fixture(autouse=True, scope='session')
def eliot_logging():
    with open("integration.eliot.json", "w") as f:
        to_file(f)
        yield


# I've mostly defined these fixtures from "easiest" to "most
# complicated", and the dependencies basically go "down the
# page". They're all session-scoped which has the "pro" that we only
# set up the grid once, but the "con" that each test has to be a
# little careful they're not stepping on toes etc :/

@pytest.fixture(scope='session')
@log_call(action_type=u"integration:reactor", include_result=False)
def reactor():
    # this is a fixture in case we might want to try different
    # reactors for some reason.
    from twisted.internet import reactor as _reactor
    return _reactor


@pytest.fixture(scope='session')
@log_call(action_type=u"integration:temp_dir", include_args=[])
def temp_dir(request) -> str:
    """
    Invoke like 'py.test --keep-tempdir ...' to avoid deleting the temp-dir
    """
    tmp = mkdtemp(prefix="tahoe")
    if request.config.getoption('keep'):
        print("\nWill retain tempdir '{}'".format(tmp))

    # I'm leaving this in and always calling it so that the tempdir
    # path is (also) printed out near the end of the run
    def cleanup():
        if request.config.getoption('keep'):
            print("Keeping tempdir '{}'".format(tmp))
        else:
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception as e:
                print("Failed to remove tmpdir: {}".format(e))
    request.addfinalizer(cleanup)

    return tmp


@pytest.fixture(scope='session')
@log_call(action_type=u"integration:flog_binary", include_args=[])
def flog_binary():
    return which('flogtool')[0]


@pytest.fixture(scope='session')
@log_call(action_type=u"integration:flog_gatherer", include_args=[])
def flog_gatherer(reactor, temp_dir, flog_binary, request):
    out_protocol = _CollectOutputProtocol()
    gather_dir = join(temp_dir, 'flog_gather')
    reactor.spawnProcess(
        out_protocol,
        flog_binary,
        (
            'flogtool', 'create-gatherer',
            '--location', 'tcp:localhost:3117',
            '--port', '3117',
            gather_dir,
        ),
        env=environ,
    )
    pytest_twisted.blockon(out_protocol.done)

    twistd_protocol = _MagicTextProtocol("Gatherer waiting at", "gatherer")
    twistd_process = reactor.spawnProcess(
        twistd_protocol,
        which('twistd')[0],
        (
            'twistd', '--nodaemon', '--python',
            join(gather_dir, 'gatherer.tac'),
        ),
        path=gather_dir,
        env=environ,
    )
    pytest_twisted.blockon(twistd_protocol.magic_seen)

    def cleanup():
        _cleanup_tahoe_process(twistd_process, twistd_protocol.exited)

        flog_file = mktemp('.flog_dump')
        flog_protocol = _DumpOutputProtocol(open(flog_file, 'w'))
        flog_dir = join(temp_dir, 'flog_gather')
        flogs = [x for x in listdir(flog_dir) if x.endswith('.flog')]

        print("Dumping {} flogtool logfiles to '{}'".format(len(flogs), flog_file))
        reactor.spawnProcess(
            flog_protocol,
            flog_binary,
            (
                'flogtool', 'dump', join(temp_dir, 'flog_gather', flogs[0])
            ),
            env=environ,
        )
        print("Waiting for flogtool to complete")
        try:
            block_with_timeout(flog_protocol.done, reactor)
        except ProcessTerminated as e:
            print("flogtool exited unexpectedly: {}".format(str(e)))
        print("Flogtool completed")

    request.addfinalizer(cleanup)

    with open(join(gather_dir, 'log_gatherer.furl'), 'r') as f:
        furl = f.read().strip()
    return furl


@pytest.fixture(scope='session')
@log_call(
    action_type=u"integration:introducer",
    include_args=["temp_dir", "flog_gatherer"],
    include_result=False,
)
def introducer(reactor, temp_dir, flog_gatherer, request):
    intro_dir = join(temp_dir, 'introducer')
    print("making introducer", intro_dir)

    if not exists(intro_dir):
        mkdir(intro_dir)
        done_proto = _ProcessExitedProtocol()
        _tahoe_runner_optional_coverage(
            done_proto,
            reactor,
            request,
            (
                'create-introducer',
                '--listen=tcp',
                '--hostname=localhost',
                intro_dir,
            ),
        )
        pytest_twisted.blockon(done_proto.done)

    config = read_config(intro_dir, "tub.port")
    config.set_config("node", "nickname", "introducer-tor")
    config.set_config("node", "web.port", "4562")
    config.set_config("node", "log_gatherer.furl", flog_gatherer)

    # "tahoe run" is consistent across Linux/macOS/Windows, unlike the old
    # "start" command.
    protocol = _MagicTextProtocol('introducer running', "introducer")
    transport = _tahoe_runner_optional_coverage(
        protocol,
        reactor,
        request,
        (
            'run',
            intro_dir,
        ),
    )
    request.addfinalizer(partial(_cleanup_tahoe_process, transport, protocol.exited))

    pytest_twisted.blockon(protocol.magic_seen)
    return TahoeProcess(transport, intro_dir)


@pytest.fixture(scope='session')
@log_call(action_type=u"integration:introducer:furl", include_args=["temp_dir"])
def introducer_furl(introducer, temp_dir):
    furl_fname = join(temp_dir, 'introducer', 'private', 'introducer.furl')
    while not exists(furl_fname):
        print("Don't see {} yet".format(furl_fname))
        sleep(.1)
    furl = open(furl_fname, 'r').read()
    tubID, location_hints, name = decode_furl(furl)
    if not location_hints:
        # If there are no location hints then nothing can ever possibly
        # connect to it and the only thing that can happen next is something
        # will hang or time out.  So just give up right now.
        raise ValueError(
            "Introducer ({!r}) fURL has no location hints!".format(
                introducer_furl,
            ),
        )
    return furl


@pytest.fixture
@log_call(
    action_type=u"integration:tor:introducer",
    include_args=["temp_dir", "flog_gatherer"],
    include_result=False,
)
def tor_introducer(reactor, temp_dir, flog_gatherer, request, tor_control_port):
    intro_dir = join(temp_dir, 'introducer_tor')
    print("making Tor introducer in {}".format(intro_dir))
    print("(this can take tens of seconds to allocate Onion address)")

    if not exists(intro_dir):
        mkdir(intro_dir)
        done_proto = _ProcessExitedProtocol()
        _tahoe_runner_optional_coverage(
            done_proto,
            reactor,
            request,
            (
                'create-introducer',
                '--tor-control-port', tor_control_port,
                '--hide-ip',
                '--listen=tor',
                intro_dir,
            ),
        )
        pytest_twisted.blockon(done_proto.done)

    # adjust a few settings
    config = read_config(intro_dir, "tub.port")
    config.set_config("node", "nickname", "introducer-tor")
    config.set_config("node", "web.port", "4561")
    config.set_config("node", "log_gatherer.furl", flog_gatherer)

    # "tahoe run" is consistent across Linux/macOS/Windows, unlike the old
    # "start" command.
    protocol = _MagicTextProtocol('introducer running', "tor_introducer")
    transport = _tahoe_runner_optional_coverage(
        protocol,
        reactor,
        request,
        (
            'run',
            intro_dir,
        ),
    )

    def cleanup():
        try:
            transport.signalProcess('TERM')
            block_with_timeout(protocol.exited, reactor)
        except ProcessExitedAlready:
            pass
    request.addfinalizer(cleanup)

    print("Waiting for introducer to be ready...")
    pytest_twisted.blockon(protocol.magic_seen)
    print("Introducer ready.")
    return transport


@pytest.fixture
def tor_introducer_furl(tor_introducer, temp_dir):
    furl_fname = join(temp_dir, 'introducer_tor', 'private', 'introducer.furl')
    while not exists(furl_fname):
        print("Don't see {} yet".format(furl_fname))
        sleep(.1)
    furl = open(furl_fname, 'r').read()
    print(f"Found Tor introducer furl: {furl} in {furl_fname}")
    return furl


@pytest.fixture(scope='session')
@log_call(
    action_type=u"integration:storage_nodes",
    include_args=["temp_dir", "introducer_furl", "flog_gatherer"],
    include_result=False,
)
def storage_nodes(reactor, temp_dir, introducer, introducer_furl, flog_gatherer, request):
    nodes_d = []
    # start all 5 nodes in parallel
    for x in range(5):
        name = 'node{}'.format(x)
        web_port=  9990 + x
        nodes_d.append(
            _create_node(
                reactor, request, temp_dir, introducer_furl, flog_gatherer, name,
                web_port="tcp:{}:interface=localhost".format(web_port),
                storage=True,
            )
        )
    nodes_status = pytest_twisted.blockon(DeferredList(nodes_d))
    nodes = []
    for ok, process in nodes_status:
        assert ok, "Storage node creation failed: {}".format(process)
        nodes.append(process)
    return nodes

@pytest.fixture(scope="session")
def alice_sftp_client_key_path(temp_dir):
    # The client SSH key path is typically going to be somewhere else (~/.ssh,
    # typically), but for convenience sake for testing we'll put it inside node.
    return join(temp_dir, "alice", "private", "ssh_client_rsa_key")

@pytest.fixture(scope='session')
@log_call(action_type=u"integration:alice", include_args=[], include_result=False)
def alice(
        reactor,
        temp_dir,
        introducer_furl,
        flog_gatherer,
        storage_nodes,
        alice_sftp_client_key_path,
        request,
):
    process = pytest_twisted.blockon(
        _create_node(
            reactor, request, temp_dir, introducer_furl, flog_gatherer, "alice",
            web_port="tcp:9980:interface=localhost",
            storage=False,
        )
    )
    pytest_twisted.blockon(await_client_ready(process))

    # 1. Create a new RW directory cap:
    cli(process, "create-alias", "test")
    rwcap = loads(cli(process, "list-aliases", "--json"))["test"]["readwrite"]

    # 2. Enable SFTP on the node:
    host_ssh_key_path = join(process.node_dir, "private", "ssh_host_rsa_key")
    accounts_path = join(process.node_dir, "private", "accounts")
    with open(join(process.node_dir, "tahoe.cfg"), "a") as f:
        f.write("""\
[sftpd]
enabled = true
port = tcp:8022:interface=127.0.0.1
host_pubkey_file = {ssh_key_path}.pub
host_privkey_file = {ssh_key_path}
accounts.file = {accounts_path}
""".format(ssh_key_path=host_ssh_key_path, accounts_path=accounts_path))
    generate_ssh_key(host_ssh_key_path)

    # 3. Add a SFTP access file with an SSH key for auth.
    generate_ssh_key(alice_sftp_client_key_path)
    # Pub key format is "ssh-rsa <thekey> <username>". We want the key.
    ssh_public_key = open(alice_sftp_client_key_path + ".pub").read().strip().split()[1]
    with open(accounts_path, "w") as f:
        f.write("""\
alice-key ssh-rsa {ssh_public_key} {rwcap}
""".format(rwcap=rwcap, ssh_public_key=ssh_public_key))

    # 4. Restart the node with new SFTP config.
    pytest_twisted.blockon(process.restart_async(reactor, request))
    pytest_twisted.blockon(await_client_ready(process))
    print(f"Alice pid: {process.transport.pid}")
    return process


@pytest.fixture(scope='session')
@log_call(action_type=u"integration:bob", include_args=[], include_result=False)
def bob(reactor, temp_dir, introducer_furl, flog_gatherer, storage_nodes, request):
    process = pytest_twisted.blockon(
        _create_node(
            reactor, request, temp_dir, introducer_furl, flog_gatherer, "bob",
            web_port="tcp:9981:interface=localhost",
            storage=False,
        )
    )
    pytest_twisted.blockon(await_client_ready(process))
    return process


@pytest.fixture(scope='session')
@pytest.mark.skipif(sys.platform.startswith('win'),
                    'Tor tests are unstable on Windows')
def chutney(reactor, temp_dir: str) -> tuple[str, dict[str, str]]:
    # Try to find Chutney already installed in the environment.
    try:
        import chutney
    except ImportError:
        # Nope, we'll get our own in a moment.
        pass
    else:
        # We already have one, just use it.
        return (
            # from `checkout/lib/chutney/__init__.py` we want to get back to
            # `checkout` because that's the parent of the directory with all
            # of the network definitions.  So, great-grand-parent.
            FilePath(chutney.__file__).parent().parent().parent().path,
            # There's nothing to add to the environment.
            {},
        )

    chutney_dir = join(temp_dir, 'chutney')
    mkdir(chutney_dir)

    missing = [exe for exe in ["tor", "tor-gencert"] if not which(exe)]
    if missing:
        pytest.skip(f"Some command-line tools not found: {missing}")

    # XXX yuck! should add a setup.py to chutney so we can at least
    # "pip install <path to tarball>" and/or depend on chutney in "pip
    # install -e .[dev]" (i.e. in the 'dev' extra)
    #
    # https://trac.torproject.org/projects/tor/ticket/20343
    proto = _DumpOutputProtocol(None)
    reactor.spawnProcess(
        proto,
        'git',
        (
            'git', 'clone',
            'https://gitlab.torproject.org/tpo/core/chutney.git',
            chutney_dir,
        ),
        env=environ,
    )
    pytest_twisted.blockon(proto.done)

    # XXX: Here we reset Chutney to a specific revision known to work,
    # since there are no stability guarantees or releases yet.
    proto = _DumpOutputProtocol(None)
    reactor.spawnProcess(
        proto,
        'git',
        (
            'git', '-C', chutney_dir,
            'reset', '--hard',
            'c4f6789ad2558dcbfeb7d024c6481d8112bfb6c2'
        ),
        env=environ,
    )
    pytest_twisted.blockon(proto.done)

    return (chutney_dir, {"PYTHONPATH": join(chutney_dir, "lib")})

@pytest.fixture(scope='session')
def tor_control_port(tor_network):
    """
    Get an endpoint description for the Tor control port for the local Tor
    network we run..
    """
    # We ignore tor_network because it can't tell us the control port.  But
    # asking for it forces the Tor network to be built before we run - so if
    # we get the hard-coded control port value correct, there should be
    # something listening at that address.
    return 'tcp:localhost:8007'

@pytest.fixture(scope='session')
@pytest.mark.skipif(sys.platform.startswith('win'),
                    reason='Tor tests are unstable on Windows')
def tor_network(reactor, temp_dir, chutney, request):
    """
    Build a basic Tor network.

    :param chutney: The root directory of a Chutney checkout and a dict of
        additional environment variables to set so a Python process can use
        it.

    :return: None
    """
    chutney_root, chutney_env = chutney
    basic_network = join(chutney_root, 'networks', 'basic')

    env = environ.copy()
    env.update(chutney_env)
    env.update({
        # default is 60, probably too short for reliable automated use.
        "CHUTNEY_START_TIME": "600",
    })
    chutney_argv = (sys.executable, '-m', 'chutney.TorNet')
    def chutney(argv):
        proto = _DumpOutputProtocol(None)
        reactor.spawnProcess(
            proto,
            sys.executable,
            chutney_argv + argv,
            path=join(chutney_root),
            env=env,
        )
        return proto.done

    # now, as per Chutney's README, we have to create the network
    pytest_twisted.blockon(chutney(("configure", basic_network)))

    # before we start the network, ensure we will tear down at the end
    def cleanup():
        print("Tearing down Chutney Tor network")
        try:
            block_with_timeout(chutney(("stop", basic_network)), reactor)
        except ProcessTerminated:
            # If this doesn't exit cleanly, that's fine, that shouldn't fail
            # the test suite.
            pass
    request.addfinalizer(cleanup)

    pytest_twisted.blockon(chutney(("start", basic_network)))

    # Wait for the nodes to "bootstrap" - ie, form a network among themselves.
    # Successful bootstrap is reported with a message something like:
    #
    # Everything bootstrapped after 151 sec
    # Bootstrap finished: 151 seconds
    # Node status:
    # test000a     :  100, done                     , Done
    # test001a     :  100, done                     , Done
    # test002a     :  100, done                     , Done
    # test003r     :  100, done                     , Done
    # test004r     :  100, done                     , Done
    # test005r     :  100, done                     , Done
    # test006r     :  100, done                     , Done
    # test007r     :  100, done                     , Done
    # test008c     :  100, done                     , Done
    # test009c     :  100, done                     , Done
    # Published dir info:
    # test000a     :  100, all nodes                , desc md md_cons ns_cons       , Dir info cached
    # test001a     :  100, all nodes                , desc md md_cons ns_cons       , Dir info cached
    # test002a     :  100, all nodes                , desc md md_cons ns_cons       , Dir info cached
    # test003r     :  100, all nodes                , desc md md_cons ns_cons       , Dir info cached
    # test004r     :  100, all nodes                , desc md md_cons ns_cons       , Dir info cached
    # test005r     :  100, all nodes                , desc md md_cons ns_cons       , Dir info cached
    # test006r     :  100, all nodes                , desc md md_cons ns_cons       , Dir info cached
    # test007r     :  100, all nodes                , desc md md_cons ns_cons       , Dir info cached
    pytest_twisted.blockon(chutney(("wait_for_bootstrap", basic_network)))

    # print some useful stuff
    try:
        pytest_twisted.blockon(chutney(("status", basic_network)))
    except ProcessTerminated:
        print("Chutney.TorNet status failed (continuing)")
