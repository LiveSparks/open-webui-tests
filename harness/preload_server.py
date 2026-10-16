"""The server `harness.preload` runs with `python -c`: it imports the heavy libraries once and
forks every scratch instance from itself. Run as `__main__`, never imported.

argv: backend, control socket path, lifeline fd, ready fd, the boot code up to the backend
import, the boot code that serves, the JSON list of modules to import.

On start it imports the modules with every `os.environ` read and write recorded, then checks it
can fork safely: one thread, no file descriptor or writable shared mapping a child would share,
no event loop. It answers on the ready fd with `{"ready": true}` or `{"disabled": reason}`. Each
connection to the control socket asks for one instance and gets `{"pid"}` with a pidfd, then
`{"status"}` once the instance exits, or `{"refused": reason}` when that instance's environment
differs where an imported library read it, so the harness starts that one the plain way.
"""

import contextlib
import ctypes
import json
import os
import selectors
import signal
import socket
import subprocess
import sys
import time

# Read by native libraries (BLAS, OpenMP, torch, Arrow, OpenSSL, libc, the interpreter) while
# the modules load, where the recorder cannot see it. Measured with a getenv interposer.
NATIVE_PREFIXES = (
    "ACC_",
    "ARROW_",
    "AZURE_LOG",
    "BLIS_",
    "CLICOLOR",
    "CRYPTOGRAPHY_",
    "CUDA",
    "DNNL_",
    "GFORTRAN_",
    "GLIBC",
    "GOMP_",
    "GOTO_",
    "I_MPI_",
    "INTEL_",
    "JE_",
    "KINETO_",
    "KMP_",
    "LANG",
    "LC_",
    "LD_",
    "MALLOC_",
    "MKL",
    "MPI_",
    "NO_COLOR",
    "NPY_",
    "NUMPY_",
    "OMP",
    "ONEDNN_",
    "OPENBLAS_",
    "OPENSSL_",
    "PSUTIL_",
    "PYTHON",
    "PYTORCH_",
    "RUST_",
    "SQLITE_",
    "SSL",
    "TMPDIR",
    "TOKENIZERS_",
    "TORCH_",
    "TZ",
    "XML_",
)
# modules that scan the whole environment at import, and the names they look at
SCANNED_PREFIXES = {"tqdm.utils": "TQDM_", "fsspec.config": "FSSPEC"}
# set here so nothing starts a thread at import; a child gets OpenBLAS's own thread count back,
# and Arrow's jemalloc runs without its purge thread, which a forked child loses anyway
OWN_SETTINGS = {"OPENBLAS_NUM_THREADS": "1", "JE_ARROW_MALLOC_CONF": "background_thread:false"}
# what the child's environment may differ in without a check
COVERAGE_SETTING = "COVERAGE_PROCESS_START"

PR_SET_PDEATHSIG = 1

OPENBLAS_PROBE = """
import json, os
import numpy, scipy.linalg
from threadpoolctl import threadpool_info
print(json.dumps({os.path.realpath(lib["filepath"]): lib["num_threads"] for lib in threadpool_info()
                  if lib["internal_api"] == "openblas"}))
"""
OPENBLAS_SETTERS = [
    f"{prefix}openblas_set_num_threads{suffix}"
    for prefix in ("", "scipy_")
    for suffix in ("", "64_")
]


class EnvRecorder:
    """Every name read or written through `os.environ` while active, with who scanned it whole."""

    def __init__(self):
        self.read: set[str] = set()
        self.written: dict[str, str | None] = {}
        self.scanned_by: set[str] = set()
        self._environ = type(os.environ)
        self._originals = {}

    @staticmethod
    def _caller() -> tuple[str, bool]:
        """The module behind this access, and whether it is iterating the whole mapping."""
        frame = sys._getframe(2)
        iterating = False
        while frame and frame.f_globals.get("__name__") in ("collections.abc", "os"):
            iterating = iterating or frame.f_code.co_name == "__iter__"
            frame = frame.f_back
        return (frame.f_globals.get("__name__", "?") if frame else "?"), iterating

    def __enter__(self):
        environ = self._environ
        originals = self._originals = {
            name: getattr(environ, name)
            for name in ("__getitem__", "__contains__", "__iter__", "__setitem__", "__delitem__")
        }
        recorder = self

        def name_of(key):
            return key.decode() if isinstance(key, bytes) else key

        def getitem(mapping, key):
            if not recorder._caller()[1]:
                recorder.read.add(name_of(key))
            return originals["__getitem__"](mapping, key)

        def contains(mapping, key):
            recorder.read.add(name_of(key))
            return originals["__contains__"](mapping, key)

        def iterate(mapping):
            recorder.scanned_by.add(recorder._caller()[0])
            return originals["__iter__"](mapping)

        def setitem(mapping, key, value):
            recorder.written[name_of(key)] = name_of(value)
            return originals["__setitem__"](mapping, key, value)

        def delitem(mapping, key):
            recorder.written[name_of(key)] = None
            return originals["__delitem__"](mapping, key)

        environ.__getitem__, environ.__contains__, environ.__iter__ = getitem, contains, iterate
        environ.__setitem__, environ.__delitem__ = setitem, delitem
        return self

    def __exit__(self, *exc):
        for name, original in self._originals.items():
            setattr(self._environ, name, original)


def thread_count() -> int:
    with open("/proc/self/status") as status:
        line = next(line for line in status if line.startswith("Threads:"))
    return int(line.split()[1])


def open_fds() -> dict[int, str]:
    fds = {}
    for name in os.listdir("/proc/self/fd"):
        try:
            fds[int(name)] = os.readlink(f"/proc/self/fd/{name}")
        except OSError:
            pass  # the listing's own descriptor, closed by now
    return fds


def openblas_paths() -> set[str]:
    with open("/proc/self/maps") as maps:
        paths = {line.split()[-1] for line in maps if "openblas" in line and ".so" in line}
    return {os.path.realpath(path) for path in paths}


def set_openblas_threads(path: str, count: int) -> None:
    library = ctypes.CDLL(path, mode=os.RTLD_NOLOAD)
    setter = next(name for name in OPENBLAS_SETTERS if hasattr(library, name))
    getattr(library, setter)(count)


def writable_shared_mappings() -> list[str]:
    with open("/proc/self/maps") as maps:
        lines = [line.split() for line in maps]
    return [" ".join(line) for line in lines if line[1][1] == "w" and line[1][3] == "s"]


def unsafe_state(allowed_fds: set[int]) -> str | None:
    """Why forking now would hand a child something it shares, or None."""
    if thread_count() != 1:
        return f"{thread_count()} threads are running"
    extra = {fd: link for fd, link in open_fds().items() if fd not in allowed_fds}
    if extra:
        return f"file descriptors would be shared: {extra}"
    shared = writable_shared_mappings()
    if shared:
        return f"writable shared memory would be shared: {shared[:3]}"
    events = sys.modules.get("asyncio.events")
    if events is None:
        return None
    policy = events._event_loop_policy
    if events._get_running_loop() or (policy and getattr(policy._local, "_loop", None)):
        return "an event loop exists"
    return None


class Preloaded:
    def __init__(self, base_env: dict[str, str], recorder: EnvRecorder, threads: dict[str, int]):
        self.base_env = base_env
        self.checked = recorder.read | set(recorder.written)
        self.written = recorder.written
        self.scanned = [SCANNED_PREFIXES[module] for module in recorder.scanned_by]
        self.openblas_threads = threads

    def refusal(self, import_env: dict[str, str]) -> str | None:
        """Why a child whose environment reads as `import_env` must not be forked, or None."""
        names = set(self.base_env) | set(import_env)
        differing = {
            name
            for name in names
            if name != COVERAGE_SETTING and self.base_env.get(name) != import_env.get(name)
        }
        for name in sorted(differing):
            if name in self.written and import_env.get(name) == self.written[name]:
                continue  # a library sets it on import and finds it set the same already
            if self.read_at_load(name):
                return f"{name} differs and a preloaded library may have read it at load"
        return None

    def read_at_load(self, name: str) -> bool:
        """Whether a preloaded library may have read `name` while it loaded."""
        return (
            name in self.checked
            or name.startswith(NATIVE_PREFIXES)
            or name.startswith(tuple(self.scanned))
        )


def preload(modules: list[str]) -> tuple[Preloaded | None, str | None]:
    base_env = dict(os.environ)
    for name in OWN_SETTINGS:
        if name in base_env:
            return None, f"{name} is set, which the preload server sets itself"
    os.environ.update(OWN_SETTINGS)
    probe = subprocess.Popen(
        [sys.executable, "-c", OPENBLAS_PROBE], env=base_env, stdout=subprocess.PIPE, text=True
    )
    with EnvRecorder() as recorder:
        for module in modules:
            try:
                __import__(module)
            except Exception as error:
                return None, f"importing {module} failed: {error!r}"
    output, _ = probe.communicate(timeout=120)
    unknown = recorder.scanned_by - set(SCANNED_PREFIXES)
    if unknown:
        return None, f"{sorted(unknown)} scan the whole environment at import"
    threads = json.loads(output) if probe.returncode == 0 else {}
    for path in openblas_paths():
        if path not in threads:
            return None, f"no plain thread count known for {path}"
    return Preloaded(base_env, recorder, threads), None


def become_instance(request: dict, preloaded: Preloaded) -> None:
    """Turn this forked child into the instance `request` asks for, as a plain start has it."""
    log_fd = os.open(request["log"], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(log_fd)
    os.environ.clear()
    os.environ.update(request["env"])
    for name, value in preloaded.written.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    os.chdir(request["cwd"])

    for path in openblas_paths():
        set_openblas_threads(path, preloaded.openblas_threads[path])
    numpy_random = sys.modules.get("numpy.random")
    if numpy_random is not None:
        numpy_random.seed()  # the global generator was seeded once, in the server
    sys.argv = ["-c", *request["argv"]]


def changed_after_import(preloaded: Preloaded, before: dict[str, str]) -> list[str]:
    """Names the backend set while it imported that a preloaded library saw otherwise."""
    return sorted(
        name
        for name in set(before) | set(os.environ)
        if before.get(name) != os.environ.get(name)
        and os.environ.get(name) != preloaded.base_env.get(name)
        and preloaded.read_at_load(name)
    )


def serve(listener: socket.socket, lifeline: int, preloaded: Preloaded) -> dict:
    """Fork an instance per request until the harness goes away; returns only in a child."""
    wake_read, wake_write = socket.socketpair()
    wake_read.setblocking(False)
    wake_write.setblocking(False)
    signal.set_wakeup_fd(wake_write.fileno())
    signal.signal(signal.SIGCHLD, lambda *_: None)
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl-C reaches the instances themselves
    selector = selectors.EpollSelector()
    selector.register(listener, selectors.EVENT_READ, "request")
    selector.register(lifeline, selectors.EVENT_READ, "lifeline")
    selector.register(wake_read, selectors.EVENT_READ, "child")
    children: dict[int, socket.socket] = {}
    server_pid = os.getpid()
    allowed_fds = {0, 1, 2, lifeline, listener.fileno(), wake_read.fileno(), wake_write.fileno()}
    allowed_fds.add(selector.fileno())

    def reap() -> None:
        while children:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                return
            connection = children.pop(pid, None)
            if connection is not None:
                with connection:
                    try:
                        connection.send(json.dumps({"status": status}).encode())
                    except OSError:
                        pass

    while True:
        for key, _ in selector.select():
            if key.data == "lifeline":
                stop_children(children)
                sys.exit(0)
            if key.data == "child":
                while True:
                    try:
                        if not wake_read.recv(256):
                            break
                    except BlockingIOError:
                        break
                reap()
                continue
            try:
                connection, _ = listener.accept()
            except OSError:
                continue
            connection.settimeout(10)
            pid = None
            try:
                request = json.loads(connection.recv(1 << 22))
                reason = preloaded.refusal(request["import_env"]) or unsafe_state(
                    allowed_fds | {connection.fileno()} | {c.fileno() for c in children.values()}
                )
                if not reason:
                    sys.stdout.flush()
                    sys.stderr.flush()
                    pid = os.fork()
            except (OSError, ValueError, KeyError) as error:
                reason = f"the request failed: {error!r}"
            if reason:
                print(f"refused: {reason}", flush=True)
                with contextlib.suppress(OSError), connection:
                    connection.send(json.dumps({"refused": reason}).encode())
                continue
            if pid == 0:
                ctypes.CDLL(None).prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
                if os.getppid() != server_pid:
                    os._exit(1)  # the server died before the death signal was set
                signal.set_wakeup_fd(-1)
                signal.signal(signal.SIGCHLD, signal.SIG_DFL)
                signal.signal(signal.SIGINT, signal.default_int_handler)
                selector.close()
                for open_socket in (
                    listener,
                    wake_read,
                    wake_write,
                    connection,
                    *children.values(),
                ):
                    open_socket.close()
                os.close(lifeline)
                return request
            children[pid] = connection
            try:
                pidfd = os.pidfd_open(pid)
                try:
                    socket.send_fds(connection, [json.dumps({"pid": pid}).encode()], [pidfd])
                finally:
                    os.close(pidfd)
            except OSError:
                os.kill(pid, signal.SIGKILL)  # nobody would ever stop it
            print(f"forked {pid} for port {request['argv'][1]}", flush=True)


def stop_children(children: dict[int, socket.socket]) -> None:
    for pid in children:
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 30
    while children and time.monotonic() < deadline:
        pid, _ = os.waitpid(-1, os.WNOHANG)
        if pid:
            children.pop(pid, None)
        else:
            time.sleep(0.1)
    for pid in children:
        os.kill(pid, signal.SIGKILL)


if __name__ == "__main__":
    BACKEND, CONTROL_PATH, LIFELINE, READY = (
        sys.argv[1],
        sys.argv[2],
        int(sys.argv[3]),
        int(sys.argv[4]),
    )
    BOOT_IMPORT, BOOT_SERVE, MODULES = sys.argv[5], sys.argv[6], json.loads(sys.argv[7])
    os.chdir(BACKEND)  # where a plain start imports from
    PRELOADED, DISABLED = preload(MODULES)
    LISTENER = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    LISTENER.bind(CONTROL_PATH)
    LISTENER.listen(16)
    if DISABLED is None:
        DISABLED = unsafe_state({0, 1, 2, LIFELINE, READY, LISTENER.fileno()})
    with os.fdopen(READY, "w") as ready:
        ready.write(json.dumps({"disabled": DISABLED} if DISABLED else {"ready": True}))
    if DISABLED:
        print(f"disabled: {DISABLED}", flush=True)
        sys.exit(0)
    REQUEST = serve(LISTENER, LIFELINE, PRELOADED)
    become_instance(REQUEST, PRELOADED)
    BEFORE_IMPORT = dict(os.environ)
    exec(compile(BOOT_IMPORT, "<string>", "exec"), globals())
    CHANGED = changed_after_import(PRELOADED, BEFORE_IMPORT)
    if CHANGED:
        print(
            f"The backend changed {CHANGED} while it imported, which a preloaded library read at "
            "import with the old value. Teach harness/preload.py that write, or set "
            "OWUI_TEST_PRELOAD=0.",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(75)  # EXIT_ENV_CHANGED in harness/preload.py
    exec(compile(BOOT_SERVE, "<string>", "exec"), globals())
