#!/bin/sh
# Sidequestor's small macOS bootstrap. The application itself comes from PyPI.
set -eu

fail() {
    printf 'Sidequestor setup stopped: %s\n' "$*" >&2
    exit 1
}

[ "$(uname -s)" = Darwin ] || fail "This installer supports macOS only."
command -v curl >/dev/null 2>&1 || fail "curl is required."

if command -v sq >/dev/null 2>&1; then
    sq_command=$(command -v sq)
    printf 'Using your existing Sidequestor installation: %s\n' "$sq_command"
else
    if command -v uv >/dev/null 2>&1; then
        uv_command=$(command -v uv)
    elif [ -x "$HOME/.local/bin/uv" ]; then
        uv_command="$HOME/.local/bin/uv"
    else
        installer=$(mktemp "${TMPDIR:-/tmp}/sidequestor-uv.XXXXXX") || fail "Could not create a temporary file."
        trap 'rm -f "$installer"' EXIT HUP INT TERM
        printf 'Installing uv, which supplies Python and the Sidequestor command…\n'
        curl -fsSL https://astral.sh/uv/install.sh -o "$installer" || fail "Could not download the uv installer."
        sh "$installer" || fail "Could not install uv."
        rm -f "$installer"
        trap - EXIT HUP INT TERM
        uv_command="$HOME/.local/bin/uv"
        [ -x "$uv_command" ] || fail "uv installed, but its command was not found at $uv_command."
    fi

    printf 'Installing Sidequestor from PyPI…\n'
    "$uv_command" tool install --python 3.11 sidequestor || fail "Could not install Sidequestor from PyPI."
    sq_command="$("$uv_command" tool dir --bin)/sq"
    [ -x "$sq_command" ] || fail "Sidequestor installed, but its sq command was not found."
    "$uv_command" tool update-shell || true
fi

registry="${XDG_CONFIG_HOME:-$HOME/.config}/yaas/instances.json"
workspace="$HOME/sidequestor"
seed_slack_app() {
    [ -n "${SIDEQUESTOR_INSTALL_SLACK_APP_ID:-}" ] || return 0
    [ -n "${SIDEQUESTOR_INSTALL_SLACK_CLIENT_ID:-}" ] || fail "Slack Client ID is missing."
    [ -n "${SIDEQUESTOR_INSTALL_SLACK_WORKSPACE_NAME:-}" ] || fail "Slack workspace name is missing."
    [ -n "${SIDEQUESTOR_INSTALL_SLACK_WORKSPACE_DOMAIN:-}" ] || fail "Slack workspace domain is missing."
    env_file="$workspace/.env"
    [ -f "$env_file" ] || fail "Workspace configuration is missing: $env_file"
    missing_keys=""
    for key in SLACK_APP_ID SLACK_CLIENT_ID SLACK_WORKSPACE_NAME SLACK_WORKSPACE_DOMAIN; do
        case "$key" in
            SLACK_APP_ID) value=$SIDEQUESTOR_INSTALL_SLACK_APP_ID ;;
            SLACK_CLIENT_ID) value=$SIDEQUESTOR_INSTALL_SLACK_CLIENT_ID ;;
            SLACK_WORKSPACE_NAME) value=$SIDEQUESTOR_INSTALL_SLACK_WORKSPACE_NAME ;;
            SLACK_WORKSPACE_DOMAIN) value=$SIDEQUESTOR_INSTALL_SLACK_WORKSPACE_DOMAIN ;;
        esac
        case "$value" in
            *[!A-Za-z0-9._-]*|'') fail "Invalid $key supplied to the installer." ;;
        esac
        if grep -q "^${key}=" "$env_file"; then
            existing=$(sed -n "s/^${key}=//p" "$env_file")
            [ "$existing" = "$value" ] || fail "Existing $key differs in $env_file; no Slack settings were changed."
        else
            missing_keys="$missing_keys $key"
        fi
    done
    for key in $missing_keys; do
        case "$key" in
            SLACK_APP_ID) value=$SIDEQUESTOR_INSTALL_SLACK_APP_ID ;;
            SLACK_CLIENT_ID) value=$SIDEQUESTOR_INSTALL_SLACK_CLIENT_ID ;;
            SLACK_WORKSPACE_NAME) value=$SIDEQUESTOR_INSTALL_SLACK_WORKSPACE_NAME ;;
            SLACK_WORKSPACE_DOMAIN) value=$SIDEQUESTOR_INSTALL_SLACK_WORKSPACE_DOMAIN ;;
        esac
        printf '%s=%s\n' "$key" "$value" >> "$env_file"
    done
    chmod 600 "$env_file"
    printf 'Slack app details are ready. Browser authorization still happens in sq setup.\n'
}
if [ -f "$registry" ]; then
    # sq resolves the saved default; do not create a competing workspace.
    printf 'Using your registered Sidequestor workspace.\n'
    "$sq_command" setup || fail "Setup is incomplete. Run sq setup again after resolving the message above."
else
    if [ ! -f "$workspace/.yaas/instance.json" ]; then
        printf 'Creating your workspace at %s…\n' "$workspace"
        "$sq_command" init "$workspace" || fail "Could not create the workspace."
    else
        printf 'Using your existing workspace at %s.\n' "$workspace"
    fi
    seed_slack_app
    "$sq_command" --workspace "$workspace" setup || fail "Setup is incomplete. Rerun sq setup to continue."
fi

# Setup starts Sidequestor; dashboard resolves its saved port, normally 8877.
"$sq_command" dashboard || fail "Setup finished, but the dashboard did not open. Run sq dashboard."
