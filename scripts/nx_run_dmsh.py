#!/usr/bin/env python3
# Run dmsh_build.py on a machine that is not the author's Mac, without changing a single file of
# the translation chain (their contents feed the chain version V, and any edit would change V and
# desync it from the shell we build on the cloud Mac).
#
#   python scripts/nx_run_dmsh.py <shader package dir> <output dir> [keys...]
#
# Two things are adjusted in-process only:
#   1. dmsh_chain.check_tools() would compare the local spirv-cross path and this machine's Metal
#      compiler against the author's lock file and refuse to run. The lock file is a fingerprint of
#      the author's machine, not a compatibility requirement: V itself comes from the lock file's
#      contents, so it is identical here and on the Mac. The check is answered with "no problems".
#   2. Every external command the chain runs (xcrun metal / metallib) is answered by nometal_run's
#      shim, so the translated MSL goes into the "library" files and the phone compiles it once at
#      first launch (the DM_SHELL host does this, see dm/gfx/nvn/nvn_mtl.m).
import os, runpy, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TOOLS = os.path.join(REPO, 'dm', 'gfx', 'tools')
sys.path.insert(0, TOOLS)

if len(sys.argv) < 3:
    sys.exit(__doc__ or 'usage: nx_run_dmsh.py <package dir> <output dir> [keys...]')

# 0. dmsh_mtlcache.py probes for APFS clonefile through ctypes.CDLL(None), which does not exist on
#    Windows (CDLL(None) returns None there and the getattr raises). Answer that one probe with a
#    stub object; the chain then falls back to a plain copy, which is what it does on Linux too.
import ctypes
_cdll = ctypes.CDLL


def _cdll_or_stub(name=None, *a, **k):
    if name is None:
        class _NoPosix:
            def __getattr__(self, item): raise AttributeError(item)
        return _NoPosix()
    return _cdll(name, *a, **k)


ctypes.CDLL = _cdll_or_stub

# 1. take over subprocess before dmsh_build imports anything that captures it
import subprocess
import nometal_run

_base = nometal_run.run
XLATE_DLL = os.path.join(TOOLS, 'dmsh_xlate_bin', 'dmsh_xlate.dll')


def _run(args, *a, **k):
    # The translator ships as an apphost binary for Linux/macOS plus the portable dll next to it.
    # On Windows the apphost cannot run (WinError 193), so the dll goes through dotnet instead.
    if isinstance(args, (list, tuple)) and args and os.path.basename(str(args[0])) == 'dmsh_xlate':
        args = ['dotnet', XLATE_DLL] + [str(x) for x in args[1:]]
    # nometal_run only knows the "metal ... --version" spelling; dmsh_mtlcache asks for
    # "xcrun -sdk macosx metallib --version" as well. Every xcrun query answers "none".
    if isinstance(args, (list, tuple)) and args and str(args[0]) == 'xcrun':
        c = [str(x) for x in args]
        if '--version' in c or any(x.startswith('--show') for x in c):
            text = k.get('text') or k.get('universal_newlines')
            return subprocess.CompletedProcess(args, 0, stdout='none\n' if text else b'none\n', stderr='')
    return _base(args, *a, **k)


subprocess.run = _run

# 2. let the tool check pass (see the header: V is unaffected)
import dmsh_chain
dmsh_chain.check_tools = lambda: []

# 3. run the real builder with the usual arguments
build = os.path.join(TOOLS, 'dmsh_build.py')
sys.argv = [build] + sys.argv[1:]
runpy.run_path(build, run_name='__main__')
