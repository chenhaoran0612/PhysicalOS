"""What the console runs on a robot for its programs and scene data (issue #43, docs/CLOUD_SKILLS.md §2).

Two fixed POSIX sh scripts, sent to ``sh -s --`` on standard input the way the upload channel of
issue #18 sends its own. Nothing a person typed is ever part of a command line or of the script
text: the directory to pack travels as an argument of hexadecimal digits and is decoded by the
script itself, every other argument is a plain name, a checksum prefix or a number.

* ``pack``: turns one directory into ``~/neuxmind-bundles/.outgoing/<token>.tar.gz``. Symbolic links
  are stored as links, never followed; ``runs/``, ``__pycache__/``, ``*.bak*``, ``*.pyc`` and the
  well-known files and directories of credentials are left out, by exact name, and the answer says
  how many and which; a directory that is itself such a place, or holds devices, pipes or sockets,
  is refused; an archive over the size limit is removed again. Read only as far as the directory goes.
* ``extract``: unpacks ``~/neuxmind-bundles/<name>/<16 digits>.tar.gz`` (put there by the upload
  channel, verified) into ``~/neuxmind-bundles/<name>/<16 digits>/`` and points
  ``~/neuxmind-bundles/<name>/current`` at it. It refuses an archive with absolute paths, ``..``,
  hard links, devices, pipes or sockets before unpacking, unpacks into a private temporary
  directory without restoring owners, then refuses any symbolic link that leads out of that
  directory. A directory of the same name that is there already is compared, not trusted: changed
  content is kept under another name. ``current`` is switched last, in one rename, and only while
  the session that asked is still there. It writes nowhere else: the directory the programs run
  from is never touched.

The scripts are written for GNU tar (Ubuntu 22.04, the target) and also run with bsdtar (macOS,
where the tests run them too). This module imports nothing of the SSH stack so that the scripts can
be exercised on a machine that has only Python and a shell.
"""
import hashlib
import re
import unicodedata

MARK = re.compile(r'^@@NX-BUNDLE:([a-z]+)(?: ([0-9a-f]{64}))?@@$', re.M)
COMMAND = 'sh -s --'
STORE = '~/neuxmind-bundles'                # everything this feature writes on a robot is below it
OUTGOING = '.outgoing'                      # a hidden name: no bundle can be called that
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024     # what an archive may grow to when unpacked
MAX_DIRECTORY_CHARS = 300
DIGITS = 16                                 # of the SHA-256, for the names on the device
TOKEN = re.compile(r'[0-9a-f]{16}')
NAME = re.compile(r'[A-Za-z0-9_+-][A-Za-z0-9._+-]{0,79}')
_ARGUMENT = re.compile(r'[A-Za-z0-9._+-]+')
_UNSEEN = ('Cc', 'Cf', 'Cs', 'Co', 'Cn', 'Zl', 'Zp')
# What a packed directory never carries. Every rule is about one name (the last part of a path), never
# about a piece of a longer name: ``secrets_loader/`` and ``credentials_helper.py`` are program files.
SKIPPED_DIRS = ('runs', '__pycache__')              # by the contract: run records and caches
SKIPPED_FILES = ('*.pyc', '*.bak*')                 # files only: a directory called maps.bak.d travels
# Well-known places and files of credentials. They are not program data and never go to the cloud.
GUARDED_DIRS = ('.ssh', '.aws', '.gnupg', '.git', '.config', '.kube', '.docker', '.azure')
GUARDED_FILES = ('.netrc', '.git-credentials', '.npmrc', '.pypirc', '.env', '.env.*', '.envrc', 'authorized_keys', 'known_hosts',
                 'id_rsa', 'id_rsa.*', 'id_dsa', 'id_dsa.*', 'id_ecdsa', 'id_ecdsa.*', 'id_ed25519', 'id_ed25519.*',
                 'credentials', 'credentials.json', 'token.json', '*.pem', '*.key')
SKIPPED = tuple(name + '/' for name in SKIPPED_DIRS) + SKIPPED_FILES
LISTED = 20                                         # names of what was left out that an answer carries
# The directory a person typed may not be one of those places (the script checks the resolved path again).
_SENSITIVE = re.compile(r'(?:^|/)(?:%s|%s)(?:/|$)' % (
    '|'.join(re.escape(name) for name in GUARDED_DIRS),
    '|'.join(re.escape(name).replace(r'\*', '[^/]*') for name in GUARDED_FILES)))
# What the upload channel of issue #18 refuses in a destination (``device_access.SENSITIVE``, repeated here
# because this module imports nothing of the SSH stack; tests/test_robot_bundle.py checks they are the same).
CHANNEL_REFUSES = re.compile(r'(?:^|/)(?:\.ssh|\.aws|\.config|\.env[^/]*|\.git|credentials?[^/]*|secrets?[^/]*|'
                             r'[^/]*(?:private[._-]?key|id_rsa|id_ed25519)[^/]*)(?:/|$)', re.I)


def _named(names):
    return ' -o '.join("-name '%s'" % name for name in names)


_SD, _SF, _GD, _GF = _named(SKIPPED_DIRS), _named(SKIPPED_FILES), _named(GUARDED_DIRS), _named(GUARDED_FILES)
# find(1) expressions, run in the directory itself. ``kept`` prints (NUL-separated) what goes into the
# archive; the other two print what stays behind, a pruned directory as one line.
_FIND = r"""kept() { find . -type d \( @SD@ -o @GD@ \) -prune -o ! -type d \( @SF@ -o @GF@ \) -o -print0; }
skipped() { find . -type d \( @GD@ \) -prune -o \( -type d \( @SD@ \) -prune -o ! -type d \( @SF@ \) ! \( @GF@ \) \) -print; }
guarded() { find . -type d \( @SD@ \) -prune -o \( -type d \( @GD@ \) -prune -o ! -type d \( @GF@ \) \) -print; }
""".replace('@SD@', _SD).replace('@SF@', _SF).replace('@GD@', _GD).replace('@GF@', _GF)
_GUARDED_PLACE = '|'.join('*/%s/*' % name for name in GUARDED_DIRS)

_DIGEST = r'''digest() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum <"$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 <"$1" | cut -d' ' -f1
  elif command -v openssl >/dev/null 2>&1; then openssl dgst -sha256 -r <"$1" | cut -d' ' -f1
  fi
}
'''
_HOME = r'''say() { echo "@@NX-BUNDLE:$1@@"; shift; [ $# -eq 0 ] || printf '%s\n' "$@"; }
[ -n "${HOME:-}" ] && [ -d "$HOME" ] || { say nohome; exit 2; }
home=$(cd "$HOME" && pwd -P) || { say nohome; exit 2; }
store=$home/neuxmind-bundles
'''
# $1: hexadecimal digits. Prints the bytes they stand for, without running anything on them.
_UNHEX = r'''unhex() {
  s=$1; out=
  while [ -n "$s" ]; do
    rest=${s#??}; pair=${s%"$rest"}; s=$rest
    n=$((0x$pair))
    out="$out\\$((n / 64))$((n % 64 / 8))$((n % 8))"
  done
  printf "$out"
}
'''
# Run by ``find … -exec sh -c "$leaving" sh <root> {} +``: prints every symbolic link among its
# arguments that leads out of <root>. A link with an absolute target does; a target that is a
# directory, or else the directory a target lies in, must be inside <root> as the file system
# resolves it; a target whose directory is missing may not climb with "..".
_LEAVING = r'''leaving='
root=$1; shift
for link do
  target=$(readlink "$link") || { printf "%s\n" "$link"; continue; }
  case $target in /*) printf "%s\n" "$link"; continue;; esac
  where=$(cd "${link%/*}" && cd "$target" 2>/dev/null && pwd -P) ||
    where=$(cd "${link%/*}" && cd "$(dirname -- "$target")" 2>/dev/null && pwd -P) || where=
  if [ -n "$where" ]; then
    case "$where/" in "$root"/*) ;; *) printf "%s\n" "$link";; esac
  else
    case "/$target/" in */../*) printf "%s\n" "$link";; esac
  fi
done'
'''
# $1 the directory in hexadecimal, $2 a token for the archive's name, $3 the size limit in bytes.
PACK_SCRIPT = _DIGEST + _HOME + _UNHEX + _LEAVING + _FIND + r'''umask 077
case $1 in ''|*[!0-9a-f]*) say refused; exit 2;; esac
[ "$((${#1} % 2))" -eq 0 ] || { say refused; exit 2; }
dir=$(unhex "$1")
case $dir in
  '~/'*) dir=$home/${dir#'~/'};;
  /*) ;;
  *) say refused; exit 2;;
esac
[ -d "$dir" ] || { say missing; exit 3; }
real=$(cd "$dir" 2>/dev/null && pwd -P) || { say unreadable; exit 3; }
# Not the home directory or anything above it, not this feature's own directory, and not a place of
# credentials: decided on the path the directory really has, whatever name led to it.
[ "$real" != / ] || { say toowide; exit 4; }
case "$home/" in "$real"/*) say toowide; exit 4;; esac
case "$real/" in "$store"/*) say inside; exit 4;; esac
case "$real/" in @GUARDED_PLACE@) say sensitive; exit 4;; esac
odd=$(cd "$real" && find . -type d \( @SKIP_DIRS@ \) -prune -o ! -type f ! -type d ! -type l -print 2>/dev/null | head -n 1)
[ -z "$odd" ] || { say special "$odd"; exit 5; }
# The store and the place for the archive must be themselves before anything is made in them.
outdir=$store/.outgoing
if [ -L "$store" ] || [ -L "$outdir" ]; then say moved; exit 8; fi
if [ -e "$store" ]; then [ "$(cd "$store" 2>/dev/null && pwd -P)" = "$store" ] || { say moved; exit 8; }; fi
mkdir -p "$outdir" 2>/dev/null
[ ! -L "$outdir" ] && [ "$(cd "$outdir" 2>/dev/null && pwd -P)" = "$outdir" ] || { say moved; exit 8; }
chmod 700 "$outdir"
# What an earlier, interrupted collection left behind.
find "$outdir" -type f -name '*.tar.gz*' -mtime +0 -exec rm -f -- {} + 2>/dev/null
out=$outdir/$2.tar.gz
cleanup() { rm -f -- "$out" "$out.rc" "$out.err" "$out.find"; }
# A session that is gone (a stop, a dropped link) must not leave the archive behind.
trap 'cleanup; exit 130' HUP INT PIPE TERM
cleanup
gnu=no; hard=
COPYFILE_DISABLE=1; export COPYFILE_DISABLE      # macOS tar: no ._ files for extended attributes
if tar --version 2>/dev/null | grep -q 'GNU tar'; then gnu=yes; hard=--hard-dereference; fi
# gzip -n: no name and no time stamp in the gzip header (bsdtar's own -z writes the time of packing),
# so the same files give the same bytes and the same checksum whenever they are packed.
( cd "$real" && kept 2>"$out.find" | tar $hard --null --no-recursion -T - -cf - 2>"$out.err"; echo $? >"$out.rc" ) |
  gzip -n 2>/dev/null | head -c "$(($3 + 1))" >"$out"
size=$(($(wc -c <"$out")))
if [ "$size" -gt "$3" ]; then cleanup; say toolarge; exit 6; fi
rc=; [ ! -f "$out.rc" ] || IFS= read -r rc <"$out.rc"
note=$(cat "$out.find" "$out.err" 2>/dev/null | head -c 600)
unread=no; [ ! -s "$out.find" ] || unread=yes
rm -f -- "$out.rc" "$out.err" "$out.find"
# GNU tar ends with 1 when a file changed while it was read: the archive is whole, and that is said.
# A part of the directory that could not be read is a failure: half a program is not a copy of it.
changed=no
case "$unread:$gnu:$rc" in
  no:*:0) ;;
  no:yes:1) changed=yes;;
  *) cleanup; say failed "$note"; exit 7;;
esac
sum=$(digest "$out")
case "$sum" in *[!0-9a-f]*) sum=;; esac
[ "${#sum}" -eq 64 ] || { cleanup; say nohash; exit 9; }
echo "size=$size" || { cleanup; exit 141; }
echo "changed=$changed"
echo "files=$(($(tar -tzf "$out" 2>/dev/null | grep -c -v '/$')))"
# Links that lead out of the directory are stored as they are; unpacking refuses an archive that has one.
echo "outside=$(($(find "$real" -type d \( @SKIP_DIRS@ \) -prune -o -type l -exec sh -c "$leaving" sh "$real" {} + 2>/dev/null | wc -l)))"
# What stayed behind: how many, and the first names, so that nobody takes the cloud copy for more than it is.
echo "skipped=$(($(cd "$real" && skipped 2>/dev/null | wc -l)))"
(cd "$real" && skipped 2>/dev/null | head -n @LISTED@ | while IFS= read -r name; do printf 'skip=%s\n' "$name"; done)
echo "guarded=$(($(cd "$real" && guarded 2>/dev/null | wc -l)))"
(cd "$real" && guarded 2>/dev/null | head -n @LISTED@ | while IFS= read -r name; do printf 'guard=%s\n' "$name"; done)
echo "@@NX-BUNDLE:packed $sum@@" || { cleanup; exit 141; }
trap - HUP INT PIPE TERM
'''.replace('@GUARDED_PLACE@', _GUARDED_PLACE).replace('@SKIP_DIRS@', _SD + ' -o ' + _GD).replace('@LISTED@', str(LISTED))
# $1 the bundle's name, $2 sixteen digits of the archive's SHA-256, $3 the most it may unpack to, in bytes.
EXTRACT_SCRIPT = _HOME + _LEAVING + r'''umask 022
base=$store/$1
archive=$base/$2.tar.gz
dest=$base/$2
tmp=$base/.extract-$2.$$
switch=$base/.current-$2.$$
[ "$(cd "$base" 2>/dev/null && pwd -P)" = "$base" ] || { say moved; exit 8; }
[ -f "$archive" ] && [ ! -L "$archive" ] || { say missing; exit 3; }
if [ -L "$dest" ] || { [ -e "$dest" ] && [ ! -d "$dest" ]; }; then say occupied "$2"; exit 4; fi
if [ -e "$base/current" ] && [ ! -L "$base/current" ]; then say occupied current; exit 4; fi
cleanup() {
  if [ -d "$tmp" ] && [ ! -L "$tmp" ]; then chmod -R u+rwX "$tmp" 2>/dev/null; rm -rf -- "$tmp"; fi
  rm -f -- "$switch"
}
# A session that is gone (a stop, a dropped link) must not go on to switch ``current``.
trap 'cleanup; exit 130' HUP INT PIPE TERM
# What an earlier, interrupted run for this archive left behind.
for old in "$base"/.extract-"$2".*; do
  [ -d "$old" ] && [ ! -L "$old" ] || continue
  chmod -R u+rwX "$old" 2>/dev/null; rm -rf -- "$old"
done
names=$(tar -tzf "$archive" 2>/dev/null) || { say damaged; exit 5; }
bad=$(printf '%s\n' "$names" | grep -E '^/|(^|/)\.\.(/|$)' | head -n 1)
[ -z "$bad" ] || { say unsafe path "$bad"; exit 6; }
kinds=$(tar -tvzf "$archive" 2>/dev/null | cut -c1 | sort -u | tr -d '\n')
case $kinds in *[!dl-]*) say unsafe kind "$kinds"; exit 6;; esac
unpacked=$(($(gzip -dc <"$archive" 2>/dev/null | head -c "$(($3 + 1))" | wc -c)))
[ "$unpacked" -le "$3" ] || { say toolarge; exit 7; }
mkdir "$tmp" || { say failed 'no temporary directory'; exit 9; }
note=$(tar -xzf "$archive" -C "$tmp" --no-same-owner --no-same-permissions 2>&1) || { cleanup; say failed "$note"; exit 9; }
chmod -R u+rwX,go-w,a-s "$tmp" 2>/dev/null
odd=$(find "$tmp" ! -type f ! -type d ! -type l -print | head -n 1)
[ -z "$odd" ] || { cleanup; say unsafe kind "${odd#"$tmp"/}"; exit 6; }
root=$(cd "$tmp" && pwd -P)
out=$(find "$root" -type l -exec sh -c "$leaving" sh "$root" {} + | head -n 1)
[ -z "$out" ] || { cleanup; say unsafe link "${out#"$root"/}"; exit 6; }
# A directory of this name from an earlier time is not taken on trust: programs run from ``current`` and
# may have changed it. The same content: nothing to do. Anything else is kept under another name and
# the cloud copy takes its place.
already=no; aside=
if [ -d "$dest" ]; then
  same=; diff --no-dereference /dev/null /dev/null >/dev/null 2>&1 && same=--no-dereference
  if diff -r $same "$tmp" "$dest" >/dev/null 2>&1; then already=yes
  else aside=$2.changed-$(date +%Y%m%d-%H%M%S); [ ! -e "$base/$aside" ] || aside=$aside.$$; fi
fi
ln -s "$2" "$switch" || { cleanup; say unplaced; exit 9; }
# Nothing was changed so far. From here on it is: only if somebody is still there to be told.
echo '@@NX-BUNDLE:ready@@' || { cleanup; exit 141; }
if [ "$already" = yes ]; then
  chmod -R u+rwX "$tmp" 2>/dev/null; rm -rf -- "$tmp"
else
  if [ -n "$aside" ]; then mv -- "$dest" "$base/$aside" || { cleanup; say failed 'could not set the changed directory aside'; exit 9; }; fi
  mv -- "$tmp" "$dest" || { cleanup; say failed 'could not place the directory'; exit 9; }
fi
# The last step, one rename: ``current`` is the old directory or the new one, never missing.
mv -T -- "$switch" "$base/current" 2>/dev/null || mv -h -- "$switch" "$base/current" 2>/dev/null || { cleanup; say unplaced; exit 9; }
trap - HUP INT PIPE TERM
echo "already=$already"
echo "aside=$aside"
echo "files=$(($(find "$dest" -type f | wc -l)))"
echo '@@NX-BUNDLE:placed@@'
'''
SCRIPTS = {'pack': PACK_SCRIPT, 'extract': EXTRACT_SCRIPT}


def command(*arguments):
    """The command line for one script: ``sh -s --`` and plain arguments. Anything else is a mistake
    in this product, never something to quote and send."""
    words = [str(argument) for argument in arguments]
    if not all(_ARGUMENT.fullmatch(word) for word in words):
        raise ValueError('bundle script argument is not a plain word')
    return ' '.join([COMMAND, *words])


def verdict(text):
    """(word, checksum or None, the lines printed after it) of the last marker line of an answer;
    ('', None, '') without one. A login script may print before ours does, so only the last marker
    counts, and only as a line of its own."""
    marks = list(MARK.finditer(text))
    if not marks:
        return '', None, ''
    return marks[-1].group(1), marks[-1].group(2), text[marks[-1].end():].strip()


def listed(text, key):
    """Every value of the ``key=value`` lines of an answer, in order (the names of what was left out)."""
    start = key + '='
    return [line[len(start):] for line in text.splitlines() if line.startswith(start)]


def fields(text):
    """The ``key=value`` lines of an answer (the last value of a key counts)."""
    return dict(line.partition('=')[::2] for line in text.splitlines() if '=' in line and not line.startswith('@@'))


def directory(value):
    """The directory a person typed, checked: ``/…`` or ``~/…``, of bounded length, with nothing
    invisible in it, no ``.`` or ``..`` on the way, and not a place where credentials live.
    Returns it without trailing slashes. Raises ValueError with the reason."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError('请填写机器人上要收进云端的目录，例如 ~/walk_programs')
    text = value.strip()
    if len(text) > MAX_DIRECTORY_CHARS:
        raise ValueError('目录太长：最多 %d 个字符' % MAX_DIRECTORY_CHARS)
    if any(unicodedata.category(char) in _UNSEEN for char in text):
        raise ValueError('目录里有看不见的字符或换行，请重新输入')
    if not text.startswith(('/', '~/')):
        raise ValueError('目录要写成完整路径（/ 开头）或主目录下的路径（~/ 开头），例如 ~/walk_programs')
    text = text.rstrip('/')
    names = text[2:].split('/') if text.startswith('~/') else text[1:].split('/')
    if not text or text == '~' or any(name in ('', '.', '..') for name in names):
        raise ValueError('目录无效：不能是主目录或根目录本身，也不能含有 . 或 .. 这样的段')
    if _SENSITIVE.search(text):
        raise ValueError('这个目录是存放登录凭据的位置，不能收进云端')
    if text == STORE or text.startswith(STORE + '/'):
        raise ValueError('%s 是从云端装下来的内容所在的位置，不能再收进云端' % STORE)
    return text


def hex_argument(text):
    """Text as the hexadecimal digits of its UTF-8 bytes: an argument no shell reads anything into."""
    return text.encode('utf-8').hex()


def plain_name(text, fallback='bundle', seed=None):
    """A name of letters, digits and ``. _ + -`` for the device, from any text (a directory's last
    part, a file name). Runs of other characters become one underscore.

    A text of which nothing plain is left, or whose name the upload channel would refuse, becomes
    ``fallback`` plus eight digits of the SHA-256 of ``seed`` (the whole directory; ``text`` without
    one): two such directories never share a place on the device."""
    text = text if isinstance(text, str) else ''
    stem = re.sub(r'[^A-Za-z0-9._+-]+', '_', text).strip('._+-')[:60].strip('._+-')
    if any(char.isalnum() for char in stem) and NAME.fullmatch(stem) and not CHANNEL_REFUSES.search(stem):
        return stem
    return '%s-%s' % (fallback, hashlib.sha256((seed if isinstance(seed, str) else text).encode('utf-8')).hexdigest()[:8])
