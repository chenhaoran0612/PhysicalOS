"""Where the upload channel may write on a device, and the three scripts it runs there (issue #18).

Allowed: a regular file below a directory the caller allows under the home directory of the SSH
login, written as ``~/dir/name``. Refused: anything else, a symbolic link anywhere on the way,
hidden names, and the places where credentials live. What is outside the login's home is either
not writable without ``sudo`` or writable by other accounts, so it cannot be allowed at all.

Names are limited to letters, digits and ``. _ + -``. A script is sent to ``sh -s`` on its
standard input and its arguments are such names only, so the command line reads the same in any
login shell (sh, bash, zsh, csh, fish) and nothing in a name can be taken for shell syntax.

The text is checked before any connection. What is a link only the device can tell: ``resolve``
looks once over SFTP, and every script looks again at the directory before it touches anything.
"""
import hashlib
import re
import stat
import unicodedata

from . import device_access

DEFAULT_ROOTS = ('~/neuxmind-pkg',)
MAX_PATH_CHARS = 1000
MAX_NAME_CHARS = 200                    # leaves room for the temporary file's suffix
MAX_ROOTS = 12
PART, UNPACKED = 'nxpart', 'nxtmp'      # the temporary file of a transfer, and its unpacked copy during the final check
NONE = '-'                              # an argument that stands for "no file" (an empty one would need quoting)
EMPTY_SHA256 = hashlib.sha256(b'').hexdigest()
MARK = re.compile(r'^@@NX-UPLOAD:([a-z]+)(?: ([0-9a-f]{64}))?@@$', re.M)
_NAME = re.compile(r'[A-Za-z0-9_+-][A-Za-z0-9._+-]*')       # one directory or file name the channel accepts
_HOME_NAME = re.compile(r'[A-Za-z0-9._+@-]+')                # one name of the home directory's own path (user@domain homes exist)
_ARGUMENT = re.compile(r'[A-Za-z0-9._+@/-]+')                # what may appear on the command line
_PLACE_ON_DEVICE = re.compile(r'(?:/[A-Za-z0-9._+@-]+)+')    # a path this channel could have produced
_UNSEEN = ('Cc', 'Cf', 'Cs', 'Co', 'Zl', 'Zp')               # control, format (direction marks too), surrogate, private, line breaks
_RESERVED = ('.%s-' % PART, '.%s-' % UNPACKED)
_HEX16 = '[0-9a-f]' * 16

# POSIX sh, read by ``sh -s`` from standard input. Nothing in them reads standard input itself.
_DIGEST = r'''digest() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum <"$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 <"$1" | cut -d' ' -f1
  elif command -v openssl >/dev/null 2>&1; then openssl dgst -sha256 -r <"$1" | cut -d' ' -f1
  fi
}
'''
# The directory was checked over SFTP before; here it must still be itself, with no link on the way.
_PLACE = r'''in_place() {
  [ "$(cd "$1" 2>/dev/null && pwd -P)" = "$1" ] || { echo '@@NX-UPLOAD:moved@@'; exit 8; }
}
'''
# $1 destination, $2 the one temporary file to keep. Only names this channel makes are removed:
# the destination's name, the kind, sixteen digits of a checksum, and for an unpacked copy a number.
_CLEAN = r'''clean() {
  stem="${1%/*}/.${1##*/}.nx"
  for f in "$stem"part-* "$stem"tmp-*; do
    [ -f "$f" ] || [ -L "$f" ] || continue
    case ${f#"$stem"} in
      part-HEX16|tmp-HEX16.[0-9]*) ;;
      *) continue;;
    esac
    [ "$f" = "$2" ] || rm -f -- "$f"
  done
}
'''.replace('HEX16', _HEX16)
# $1 destination, $2 its expected size, $3 temporary file of this transfer.
# The checksum tool is tried on empty input; the destination is hashed only when its size already fits.
PROBE_SCRIPT = _DIGEST + _PLACE + _CLEAN + r'''in_place "${1%/*}"
clean "$1" "$3"
echo "hash=$(digest /dev/null)"
if command -v gzip >/dev/null 2>&1; then echo gzip=yes; else echo gzip=no; fi
if [ -f "$1" ] && [ ! -L "$1" ] && [ "$(($(wc -c <"$1")))" -eq "$2" ]; then echo "dest=$(digest "$1")"; else echo dest=; fi
echo '@@NX-UPLOAD:probed@@'
'''
# $1 temporary file, $2 destination, $3 expected SHA-256, $4 gzip|identity, $5 mode, $6 prefix for the unpacked copy.
# A wrong checksum or a damaged stream removes what was uploaded. Nothing else does: a checksum
# that could not be computed, or a disk too full to unpack, says nothing about the content. The
# stream is tested first (that needs no room), so that a failure to unpack is not taken for damage.
# The unpacked copy is made under a name nothing else holds, readable by this account only.
FINALIZE_SCRIPT = _DIGEST + _PLACE + r'''umask 077
part=$1; dest=$2; want=$3; tmp="$6.$$"
in_place "${dest%/*}"
[ -f "$part" ] && [ ! -L "$part" ] || { echo '@@NX-UPLOAD:missing@@'; exit 4; }
if [ -d "$dest" ] || [ -L "$dest" ]; then echo '@@NX-UPLOAD:unplaced@@'; echo 'a directory or a link is in the place of the file'; exit 7; fi
if [ "$4" = gzip ]; then
  note=$(gzip -t <"$part" 2>&1) || { rm -f -- "$part"; echo '@@NX-UPLOAD:damaged@@'; printf '%s\n' "$note"; exit 3; }
  (set -C; : >"$tmp") 2>/dev/null || { echo '@@NX-UPLOAD:unplaced@@'; echo 'the name for the unpacked copy is taken'; exit 7; }
  note=$(gzip -dc <"$part" 2>&1 >"$tmp") || { rm -f -- "$tmp"; echo '@@NX-UPLOAD:full@@'; printf '%s\n' "$note"; exit 9; }
  ready=$tmp
else
  ready=$part
fi
got=$(digest "$ready")
case "$got" in *[!0-9a-f]*) got=;; esac
[ "${#got}" -eq 64 ] || { rm -f -- "$tmp"; echo '@@NX-UPLOAD:nohash@@'; exit 6; }
[ "$got" = "$want" ] || { rm -f -- "$tmp" "$part"; echo "@@NX-UPLOAD:mismatch $got@@"; exit 5; }
note=$({ chmod "$5" "$ready" && mv -f -- "$ready" "$dest"; } 2>&1) || {
  chmod 600 "$part" 2>/dev/null; rm -f -- "$tmp"; echo '@@NX-UPLOAD:unplaced@@'; printf '%s\n' "$note"; exit 7; }
rm -f -- "$part" "$tmp"
echo "@@NX-UPLOAD:ok $got@@"
'''
# $1 destination.
CLEAN_SCRIPT = _PLACE + _CLEAN + r'''in_place "${1%/*}"
clean "$1" -
echo '@@NX-UPLOAD:cleaned@@'
'''
SCRIPTS = {'probe': PROBE_SCRIPT, 'finalize': FINALIZE_SCRIPT, 'clean': CLEAN_SCRIPT}
COMMAND = 'sh -s --'


class TransferError(ValueError):
    """A transfer that was refused or did not finish.

    ``stage``: 'request' (refused before anything was written), 'connect', 'send' or 'verify'.
    ``sent`` of ``total`` bytes of the file are confirmed on the device. ``remote_temp`` is the
    unfinished file left there (None: nothing is left), and ``resumable`` says whether calling
    again goes on from ``sent``."""

    def __init__(self, stage, reason, message, sent=0, total=0, remote_temp=None, resumable=False, attempts=0):
        super().__init__(message)
        self.stage, self.reason, self.sent, self.total = stage, reason, sent, total
        self.remote_temp, self.resumable, self.attempts = remote_temp, resumable, attempts


def refuse(reason, message):
    return TransferError('request', reason, message)


def command(*arguments):
    """The command line for one script: ``sh -s --`` and its arguments. An argument that is not a
    plain name or path is a mistake in this product, never something to quote and send."""
    words = [str(argument) for argument in arguments]
    if not all(_ARGUMENT.fullmatch(word) for word in words):
        raise ValueError('upload script argument is not a plain name or path')
    return ' '.join([COMMAND, *words])


def verdict(text):
    """(word, checksum or None, what was printed after it) of the last marker line in a script's
    answer; ('', None, '') without one. A login script may print before ours does, so only the
    last marker counts, and only as a line of its own."""
    marks = list(MARK.finditer(text))
    if not marks:
        return '', None, ''
    return marks[-1].group(1), marks[-1].group(2), text[marks[-1].end():].strip()


def shown(text, limit=300):
    """Text that came from a device or from disk, made fit to show: nothing invisible, nothing
    that moves the cursor, breaks the line or changes the direction of the text around it."""
    visible = ''.join(' ' if unicodedata.category(char) in _UNSEEN else char for char in device_access.redact(str(text)))
    return visible.strip()[:limit]


def place_on_device(path):
    """True for text that can be a path this channel made on a device (as read back from a record)."""
    return isinstance(path, str) and len(path) <= MAX_PATH_CHARS + MAX_NAME_CHARS and bool(_PLACE_ON_DEVICE.fullmatch(path))


def temp_name(dest, kind, sha256):
    """A hidden sibling of the destination. A destination name never starts with a dot, so no
    upload can be aimed at one of these; the checksum keeps versions of a file apart."""
    directory, _, name = dest.rpartition('/')
    return '%s/.%s.%s-%s' % (directory, name, kind, sha256[:16])


def _names(path, reason, what):
    """The names of ``~/a/b``: ('a', 'b'). Each one plain, none hidden."""
    if not isinstance(path, str) or not path.startswith('~/') or len(path) > MAX_PATH_CHARS:
        raise refuse(reason, what + '无效：要写成 ~/目录/文件名（登录账号主目录下的位置），最多 %d 个字符' % MAX_PATH_CHARS)
    names = tuple(path[2:].split('/'))
    if not all(_NAME.fullmatch(name) and len(name) <= MAX_NAME_CHARS for name in names):
        raise refuse(reason, what + '无效：每一段只能用字母、数字和 . _ + -，不能以 . 开头，最多 %d 个字符' % MAX_NAME_CHARS)
    return names


def path_names(remote_path):
    """The names of a destination written as ``~/dir/name``; raises if it is not written that way."""
    return _names(remote_path, 'path', '目标路径')


def check_roots(roots):
    """The directories a caller allows, as name tuples below the home directory."""
    if not isinstance(roots, (tuple, list)) or not 1 <= len(roots) <= MAX_ROOTS:
        raise refuse('roots', '允许写入的目录无效：需要 1–%d 个 ~/ 开头的目录' % MAX_ROOTS)
    allowed = tuple(_names(root, 'roots', '允许写入的目录') for root in roots)
    if any(device_access.SENSITIVE.search(root) for root in roots):
        raise refuse('roots', '允许写入的目录无效：不能是存放凭据的位置')
    return allowed


def check_remote_path(remote_path, allowed):
    """Everything about a destination that its text decides. Returns its names below the home directory."""
    names = path_names(remote_path)
    if device_access.SENSITIVE.search(remote_path):
        raise refuse('sensitive', '目标路径无效：不能写入存放凭据的位置')
    if any(mark in names[-1] for mark in _RESERVED):
        raise refuse('path', '目标文件名无效：%s 和 %s 留给传输中的临时文件' % _RESERVED)
    if not any(len(names) > len(root) and names[:len(root)] == root for root in allowed):
        raise refuse('outside', '目标路径不在允许写入的目录内：' + remote_path)
    return names


def mode_text(mode):
    if isinstance(mode, bool) or not isinstance(mode, int) or mode < 0 or mode & ~0o755:
        raise refuse('mode', '文件权限无效：只能是 0o755 以内的权限（别的用户不可写，不带特殊位）')
    return '%o' % mode


def lstat(sftp, path):
    """What is at ``path``, not following a link; None when nothing is. A server that does not say
    what kind of thing it is cannot be worked with: no check here would mean anything."""
    try:
        info = sftp.lstat(path)
    except FileNotFoundError:
        return None
    if isinstance(info.st_mode, bool) or not isinstance(info.st_mode, int):
        raise TransferError('connect', 'no_sftp', '设备的 SFTP 服务没有报告文件类型，无法确认目标位置，所以没有传输')
    return info


def resolve(sftp, names, create=True):
    """The destination as the device sees it, or None when ``create`` is off and a directory is
    missing. Below the home directory nothing on the way may be a symbolic link; missing
    directories are made on the way down."""
    home = sftp.normalize('.')
    plain = isinstance(home, str) and home.startswith('/') and len(home) <= MAX_PATH_CHARS and \
        all(_HOME_NAME.fullmatch(name) and name not in ('.', '..') for name in home[1:].split('/'))
    if not plain:
        raise refuse('outside', '登录账号的主目录不可用（没有主目录，或路径里有字母、数字和 . _ + - 以外的字符），无法确定允许写入的位置')
    current = home
    for name in names[:-1]:
        current += '/' + name
        info = lstat(sftp, current)
        if info is None:
            if not create:
                return None
            sftp.mkdir(current, 0o755)
        elif stat.S_ISLNK(info.st_mode):
            raise refuse('outside', '目标路径经过符号链接，可能指向允许目录之外：' + current)
        elif not stat.S_ISDIR(info.st_mode):
            raise refuse('occupied', '目标路径上的这一段不是目录：' + current)
    if sftp.normalize(current) != current:
        raise refuse('outside', '设备把目标目录解析到了别处：' + current)
    dest = current + '/' + names[-1]
    info = lstat(sftp, dest)
    if info is not None and not stat.S_ISREG(info.st_mode):
        raise refuse('occupied', '目标位置已有同名的目录或链接，不会覆盖：' + dest)
    return dest
