#!/bin/sh
# Testing hook: DIGGER_PACKAGE accepts a wheel path or requirement.
# Default: cosine-digger==$DIGGER_VERSION
set -eu

DIGGER_VERSION=0.4.0
UV_VERSION=0.11.16
DIGGER_PACKAGE=${DIGGER_PACKAGE:-cosine-digger==$DIGGER_VERSION}

fail() {
    printf '  ✗ %s: %s\n' "$1" "$2"
    printf 'Check your connection and run the same command again. Steps marked ✓ are done.\n'
    exit 1
}

display_path() {
    case "$1" in
        "$HOME") printf '~' ;;
        "$HOME"/*) printf '~/%s' "${1#"$HOME"/}" ;;
        *) printf '%s' "$1" ;;
    esac
}

usable_uv() {
    uv_version=$("$1" --version 2>/dev/null) || return 1
    uv_version=$(printf '%s\n' "$uv_version" | awk '
        $1 == "uv" && $2 ~ /^[0-9]+\.[0-9]+\.[0-9]+$/ {
            split($2, v, ".")
            if (v[1] > 0 || v[2] >= 5) print $2
        }')
    [ -n "$uv_version" ]
}

uv_bin=${XDG_DATA_HOME:+$XDG_DATA_HOME/../bin}
uv_bin=${UV_INSTALL_DIR:-${XDG_BIN_HOME:-${uv_bin:-$HOME/.local/bin}}}
uv=
for candidate in "$(command -v uv || :)" "${UV_TOOL_BIN_DIR:-$HOME/.local/bin}/uv" "$uv_bin/uv"; do
    if [ -n "$candidate" ] && usable_uv "$candidate"; then
        uv=$candidate
        break
    fi
done

announced=false
if [ -z "$uv" ]; then
    printf 'Installing Digger %s. No sudo needed.\n' "$DIGGER_VERSION"
    announced=true
    # Check curl separately: POSIX pipelines only report the last command's status.
    installer=$(curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh") || fail uv 'download failed'
    printf '%s\n' "$installer" | env UV_NO_MODIFY_PATH=1 sh >/dev/null 2>&1 || fail uv 'installation failed'
    uv=$uv_bin/uv
    usable_uv "$uv" || fail uv 'installation failed'
fi

bin_dir=$("$uv" tool dir --bin 2>/dev/null) || fail "Digger $DIGGER_VERSION" 'could not find the bin folder'
case "$bin_dir" in /*) ;; *) fail "Digger $DIGGER_VERSION" 'invalid bin folder' ;; esac
bin_display=$(display_path "$bin_dir")
tool_list=$("$uv" tool list 2>/dev/null) || fail "Digger $DIGGER_VERSION" 'could not check installed tools'
installed=$(printf '%s\n' "$tool_list" | awk '$1 == "cosine-digger" {sub(/^v/, "", $2); print $2; exit}')
if [ -n "$installed" ]; then
    printf 'Digger %s is already installed.\n' "$installed"
else
    if [ "$announced" = false ]; then
        printf 'Installing Digger %s. No sudo needed.\n' "$DIGGER_VERSION"
    fi
    printf '  ✓ uv %s\n' "$uv_version"
    if [ -e "$bin_dir/cosine-digger" ] || [ -L "$bin_dir/cosine-digger" ]; then
        printf '  ✗ Digger %s: another program is already installed as %s/cosine-digger\n' "$DIGGER_VERSION" "$bin_display"
        printf 'Rename or remove that file, then run the same command again. Steps marked ✓ are done.\n'
        exit 1
    fi
    # --no-bin keeps a python3.12 command out of the bin folder; uv before 0.8 has no
    # such flag and installs no command anyway. One attempt: a failure is never retried
    # with other flags.
    case "$uv_version" in
        0.[0-7].*) no_bin= ;;
        *) no_bin=--no-bin ;;
    esac
    # $no_bin is unquoted on purpose: empty for uv before 0.8.
    "$uv" python install 3.12 $no_bin >/dev/null 2>&1 || fail 'Python 3.12' 'download failed'
    printf '  ✓ Python 3.12\n'
    "$uv" tool install --python 3.12 "$DIGGER_PACKAGE" >/dev/null 2>&1 || fail "Digger $DIGGER_VERSION" 'installation failed'
    printf '  ✓ Digger %s → %s/cosine-digger\n' "$DIGGER_VERSION" "$bin_display"
fi

case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *)
        escaped_bin=$(printf '%s\n' "$bin_dir" | sed 's/[\\"$`]/\\&/g')
        path_line="export PATH=\"$escaped_bin:\$PATH\""
        # The file a new Terminal's shell reads: zsh reads .zshrc; bash in a macOS Terminal is a
        # login shell (the first of .bash_profile, .bash_login and .profile), elsewhere an
        # interactive one (.bashrc); fish reads its conf.d folder; other shells read .profile.
        case "${SHELL:-}" in
            */zsh) rc=$HOME/.zshrc ;;
            */bash)
                rc=$HOME/.bashrc
                if [ "$(uname -s 2>/dev/null)" = Darwin ]; then
                    rc=$HOME/.profile
                    for login_file in "$HOME/.bash_profile" "$HOME/.bash_login"; do
                        if [ -e "$login_file" ]; then
                            rc=$login_file
                            break
                        fi
                    done
                fi
                ;;
            */fish)
                rc=${XDG_CONFIG_HOME:-$HOME/.config}/fish/conf.d/cosine-digger.fish
                fish_bin=$(printf '%s\n' "$bin_dir" | sed 's/[\\"$]/\\&/g')
                path_line="contains -- \"$fish_bin\" \$PATH; or set -gx PATH \"$fish_bin\" \$PATH"
                ;;
            *) rc=$HOME/.profile ;;
        esac
        rc_display=$(display_path "$rc")
        if ! grep -Fqx "$path_line" "$rc" 2>/dev/null; then
            if (mkdir -p "$(dirname "$rc")" && printf '\n# Added by the Digger installer.\n%s\n' "$path_line" >> "$rc") 2>/dev/null; then
                printf 'Added %s to your PATH in %s. New Terminal windows will find cosine-digger.\n' "$bin_display" "$rc_display"
            else
                # The line as it would have been added (an absolute path: "~" doesn't expand inside
                # quotes), and a command that runs as printed.
                case "$bin_display" in
                    *[!A-Za-z0-9._/~+-]*) run="'$(printf '%s' "$bin_dir/cosine-digger" | sed "s/'/'\\\\''/g")'" ;;
                    *) run=$bin_display/cosine-digger ;;
                esac
                printf "Couldn't add %s to your PATH. Add this line to %s, then open a new Terminal:\n" "$bin_display" "$rc_display"
                printf '  %s\n' "$path_line"
                printf 'Until then, run %s start\n' "$run"
            fi
        fi
        ;;
esac

exec "$bin_dir/cosine-digger" start
