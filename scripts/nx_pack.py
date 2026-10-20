#!/usr/bin/env python3
# Pack a playable IPA on Windows (or Linux / macOS) from the shell plus your own game files.
#
#   python scripts/nx_pack.py <shell.zip> <game dir> [-o out.ipa]
#
# <shell.zip>  built once by dm/ios/tools/shell_build.sh (needs macOS + Xcode); it holds the
#              complete app but no game content.
# <game dir>   the folder nx_extract wrote: ExeFS/, RomFS/, control.nacp, icon.jpg
#
# What this does, in order (every step is skipped when its output is already there):
#   1. unpack the shell, check its version info against the game profile
#   2. rewrite the code modules (dm/tools/patch_nso.py) -> *.patched.bin + fixups/gates/...
#   3. write Frameworks/Guest.framework/Guest from them (dm/tools/guest_dylib.py)
#   4. split the shaders (dm/gfx/tools/shader_split.py) and translate them to Metal
#      (dmsh_build.py under nometal_run.py, so the MSL source goes into the "libraries" and the
#      phone compiles them once on first launch; no Apple shader compiler needed here)
#   5. drop in the RomFS, the rewriter tables, the icon, and the real name/version
#   6. pack the IPA (tools/pack_ipa.py)
#
# Needs: python 3.9+ with lz4 capstone fonttools zstandard, the .NET SDK (for the translator),
# and spirv-cross on PATH. Nothing here calls Xcode, a compiler or Apple tooling.
import argparse, json, os, plistlib, re, shutil, subprocess, sys, time, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DM = os.path.join(REPO, 'dm')
PY = sys.executable


def say(s):
    print('[%s] %s' % (time.strftime('%H:%M:%S'), s), flush=True)


def die(s):
    sys.exit('\n[x] ' + s)


def tool_env():
    """Folders the chain needs on PATH: spirv-cross (MSYS2's mingw64/bin on Windows) and dotnet."""
    extra = []
    if sys.platform == 'win32':
        for p in (r'C:\msys64\mingw64\bin', os.path.expanduser(r'~\msys64\mingw64\bin'),
                  r'C:\Program Files\dotnet'):
            if os.path.isdir(p):
                extra.append(p)
    if not extra:
        return {}
    return {'PATH': os.pathsep.join(extra + [os.environ.get('PATH', '')])}


def preflight():
    """Stop with one clear message instead of failing inside the shader stage."""
    import shutil as _sh
    par = os.environ.get('PATH', '')
    for p in tool_env().get('PATH', '').split(os.pathsep):
        if p and os.path.isdir(p) and p not in par:
            os.environ['PATH'] = os.environ.get('PATH', '') + os.pathsep + p
    missing = [n for n in ('spirv-cross', 'dotnet') if not _sh.which(n)]
    if missing:
        die('not on PATH: %s\n'
            '  spirv-cross: install MSYS2 (winget install MSYS2.MSYS2) then\n'
            '               C:\\msys64\\usr\\bin\\pacman.exe -S --needed mingw-w64-x86_64-spirv-cross\n'
            '  dotnet:      winget install Microsoft.DotNet.SDK.10' % ', '.join(missing))
    for mod in ('lz4.block', 'capstone', 'fontTools', 'zstandard'):
        try:
            __import__(mod)
        except ImportError:
            die('python module missing: %s (pip install lz4 capstone fonttools zstandard)' % mod)


def run(cmd, env=None, what=None):
    e = dict(os.environ)
    e.setdefault('PYTHONUTF8', '1')
    e.update(tool_env())
    e.update(env or {})
    r = subprocess.run(cmd, env=e, text=True, capture_output=True)
    if r.returncode != 0:
        tail = '\n'.join((r.stdout or '').splitlines()[-20:] + (r.stderr or '').splitlines()[-20:])
        die('%s failed (exit %d)\n%s' % (what or ' '.join(cmd[:3]), r.returncode, tail))
    return r.stdout


def profile_dir(title_id):
    d = os.path.join(DM, 'games', title_id)
    if not os.path.isdir(d):
        die('no game profile %s in dm/games/ — this build only handles games the repository has a '
            'profile for' % title_id)
    return d


def read_tsv(path):
    out = {}
    for line in open(path, encoding='utf-8'):
        if line.startswith('#') or '\t' not in line:
            continue
        k, v = line.rstrip('\n').split('\t', 1)
        out.setdefault(k, v)
    return out


def read_info(path):
    info = {}
    for line in open(path, encoding='utf-8'):
        line = line.rstrip('\n')
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            info[k.strip()] = v.strip()
    return info


def nacp_name_version(nacp):
    """Name (first language entry) and display version from a control.nacp."""
    if not os.path.exists(nacp):
        return None, None
    b = open(nacp, 'rb').read()
    name = b[0:0x200].split(b'\0')[0].decode('utf-8', 'replace').strip()
    ver = b[0x3060:0x3070].split(b'\0')[0].decode('ascii', 'replace').strip()
    return (name or None), (ver or None)


def main():
    ap = argparse.ArgumentParser(description='Pack an IPA from the shell and your own game files')
    ap.add_argument('shell', help='nxnative-shell-<title id>.zip built by dm/ios/tools/shell_build.sh')
    ap.add_argument('game', help='folder holding ExeFS/ and RomFS/ (made by nx_extract)')
    ap.add_argument('-o', '--out', help='output .ipa (default: <game>/SuperMarioOdyssey.ipa)')
    ap.add_argument('-w', '--work', help='work folder (default: <game>/.nxpack)')
    ap.add_argument('--keep', action='store_true', help='keep the unpacked shell in the work folder')
    a = ap.parse_args()
    preflight()

    shell, game = os.path.abspath(a.shell), os.path.abspath(a.game)
    [os.path.exists(shell) or die('no shell: %s' % shell)]
    exefs, romfs = os.path.join(game, 'ExeFS'), os.path.join(game, 'RomFS')
    [os.path.isdir(exefs) or die('no %s (run unpack-smo.ps1 first)' % exefs)]
    [os.path.isdir(romfs) or die('no %s' % romfs)]

    work = os.path.abspath(a.work or os.path.join(game, '.nxpack'))
    # The bundle must keep its .app suffix inside Payload/ — iOS and every sideloading tool
    # refuse an IPA whose bundle is not Payload/<name>.app.
    app = os.path.join(work, 'DmIOS.app')
    patched = os.path.join(work, 'patched')
    shdr = os.path.join(work, 'shader')
    msl = os.path.join(work, 'msl')
    for d in (work, patched, shdr, msl):
        os.makedirs(d, exist_ok=True)

    # --- 1. unpack the shell and read what it says about itself
    info_path = os.path.join(app, 'dm', 'shell_info.txt')
    if not os.path.exists(info_path):
        shutil.rmtree(app, ignore_errors=True)
        shutil.rmtree(os.path.join(work, 'Payload'), ignore_errors=True)
        say('unpacking the shell')
        with zipfile.ZipFile(shell) as z:
            z.extractall(work)
        if not os.path.isdir(app):
            # An IPA-style shell keeps the app under Payload/; a flat one may not.
            cand = []
            for base in (os.path.join(work, 'Payload'), work):
                if os.path.isdir(base):
                    cand += [os.path.join(base, n) for n in os.listdir(base) if n.endswith('.app')]
            if not cand:
                die('the shell has no <name>.app inside')
            os.rename(cand[0], app)
        if not os.path.exists(info_path):
            die('the shell has no dm/shell_info.txt: it was not built by shell_build.sh')
    info = read_info(info_path)
    title = info.get('GAME') or die('the shell does not name its game')
    prof = profile_dir(title)
    gtsv = read_tsv(os.path.join(prof, 'game.tsv'))
    modules = info.get('MODULES', gtsv.get('modules', '')).split()
    if not modules:
        die('the shell does not say which code modules to rewrite')
    chain = info.get('CHAIN', '')
    msldir = info.get('MSLDIR', 'b-%s' % chain)
    say('shell: %s  modules: %s  chain: %s' % (title, ' '.join(modules), chain))

    # --- 2. rewrite the code modules from the player's own files
    for m in modules:
        src = os.path.join(exefs, m)
        if not os.path.exists(src):
            die('%s is missing from %s' % (m, exefs))
    if not all(os.path.exists(os.path.join(patched, m + '.fixups.tsv')) for m in modules):
        say('rewriting %s' % ' '.join(modules))
        for m in modules:
            run([PY, os.path.join(DM, 'tools', 'patch_nso.py'), os.path.join(exefs, m), patched, m],
                what='patch_nso.py %s' % m)
    else:
        say('rewrite reused')

    # --- 3. the dynamic library the shell's loader takes the modules from
    fw = os.path.join(app, 'Frameworks', 'Guest.framework')
    if not os.path.exists(os.path.join(fw, 'Guest')):
        say('writing Frameworks/Guest.framework')
        os.makedirs(os.path.join(app, 'Frameworks'), exist_ok=True)
        run([PY, os.path.join(DM, 'tools', 'guest_dylib.py'), patched, os.path.join(app, 'Frameworks')] + modules,
            what='guest_dylib.py')
    else:
        say('Guest.framework reused')

    # --- 4. shaders: split the game's packages, then translate to MSL
    pkg = os.path.join(shdr, 'pkg')
    if not os.path.isdir(pkg) or not os.listdir(pkg):
        say('splitting the shaders out of the RomFS (a few minutes)')
        run([PY, os.path.join(DM, 'gfx', 'tools', 'shader_split.py'),
             romfs, os.path.join(exefs, 'main'), pkg,
             os.path.join(shdr, 'shader_map.tsv'), os.path.join(shdr, 'shader_facts.tsv'),
             os.path.join(shdr, 'shader_pairs.tsv')], what='shader_split.py')
    else:
        say('shader split reused')

    libdir = os.path.join(msl, 'b-' + chain)
    if not os.path.isdir(libdir) or not any(n.endswith(('_b.metallib',)) for n in os.listdir(libdir)):
        say('translating shaders to Metal Shading Language (the phone compiles them on first launch)')
        env = {'DM_GAME': title, 'IOSOUT': msl, 'GAME': title}
        run([PY, os.path.join(HERE, 'nx_run_dmsh.py'), pkg, msl],
            env=env, what='dmsh_build.py (nometal mode)')
    else:
        say('shader libraries reused')

    # --- 5. drop the game content into the app
    say('installing RomFS, shader libraries, rewriter tables, icon')
    dst_romfs = os.path.join(app, 'romfs')
    shutil.rmtree(dst_romfs, ignore_errors=True)
    shutil.copytree(romfs, dst_romfs)
    dst_msl = os.path.join(app, 'msl')
    shutil.rmtree(dst_msl, ignore_errors=True)
    os.makedirs(dst_msl, exist_ok=True)
    if os.path.isdir(libdir):
        shutil.copytree(libdir, os.path.join(dst_msl, msldir), dirs_exist_ok=True)
    if os.path.isdir(os.path.join(msl, 'vk')):
        shutil.copytree(os.path.join(msl, 'vk'), os.path.join(dst_msl, 'vk'), dirs_exist_ok=True)
    dmout = os.path.join(app, 'dm')
    for f in os.listdir(patched):
        if f.endswith('.tsv'):
            shutil.copyfile(os.path.join(patched, f), os.path.join(dmout, f))
    # The app's real name and version come from the player's control data
    nacp = os.path.join(game, 'control.nacp')
    name, ver = nacp_name_version(nacp)
    icon = os.path.join(game, 'icon.jpg')

    # --- 6. the IPA
    out = os.path.abspath(a.out or os.path.join(game, gtsv.get('ipa', 'game.ipa')))
    plist = os.path.join(app, 'Info.plist')
    if os.path.exists(plist):
        with open(plist, 'rb') as f:
            P = plistlib.load(f)
        if name:
            P['CFBundleDisplayName'] = name
            P['CFBundleName'] = name
        if ver:
            P['CFBundleShortVersionString'] = ver
            P['CFBundleVersion'] = ver
        if icon and os.path.exists(icon):
            shutil.copyfile(icon, os.path.join(app, 'icon.jpg'))
            P['CFBundleIcons'] = {'CFBundlePrimaryIcon': {'CFBundleIconFiles': ['icon.jpg']}}
        with open(plist, 'wb') as f:
            plistlib.dump(P, f, fmt=plistlib.FMT_BINARY)
        say('app: %s %s' % (name or '?', ver or '?'))

    say('packing %s' % out)
    store = gtsv.get('zip_store', '')
    cmd = [PY, os.path.join(DM, 'ios', 'tools', 'pack_ipa.py'), out]
    if store:
        cmd += ['--store', store]
    cmd += ['--skip', 'Payload/' + os.path.basename(app) + '/_CodeSignature',
            '--skip', 'Payload/' + os.path.basename(app) + '/embedded.mobileprovision',
            'Payload/%s=%s' % (os.path.basename(app), app)]
    run(cmd, what='pack_ipa.py')
    if not a.keep:
        shutil.rmtree(app, ignore_errors=True)
    say('done: %s (%s)' % (out, '%.2f GB' % (os.path.getsize(out) / 2 ** 30)))
    print('''
Next: sideload it with Sideloadly (Windows) using a free Apple ID.
  - the certificate lasts 7 days; re-sideload to refresh (the save lives in the app's Documents)
  - first launch compiles every shader once (progress on screen, several minutes)
  - Chinese UI: put DM_LANG=zh-Hans in Documents/env.txt (Files app → the app's folder)''')


if __name__ == '__main__':
    main()
