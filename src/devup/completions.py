"""Shell autocompletion script generators for bash, zsh, fish, and powershell."""

from __future__ import annotations

SUPPORTED_SHELLS = ("bash", "zsh", "fish", "powershell")

_BASH_COMPLETION = r"""# bash completion for devup
_devup_complete() {
    local cur prev words cword
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    local subcommands="list ls run pick init validate doctor completion"
    local flags="--help --version --cwd --no-workspaces --timestamps --no-prefix --keep-going --dry-run --watch --env-file --no-dotenv --no-port-check --strict-ports --check-updates --no-update-check --color --no-color --json --force --template --interactive --yes --online"

    if [[ "${prev}" == "completion" ]]; then
        COMPREPLY=( $(compgen -W "bash zsh fish powershell" -- "${cur}") )
        return 0
    fi
    if [[ "${prev}" == "--template" ]]; then
        COMPREPLY=( $(compgen -W "auto fullstack python-node java-microservices docker-compose" -- "${cur}") )
        return 0
    fi
    if [[ "${cur}" == -* ]]; then
        COMPREPLY=( $(compgen -W "${flags}" -- "${cur}") )
        return 0
    fi

    local tasks=""
    if command -v python3 >/dev/null 2>&1; then
        tasks="$(devup list --json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(' '.join(list(d.get('groups',{}).keys()) + list(d.get('tasks',{}).keys())))" 2>/dev/null)"
    fi
    COMPREPLY=( $(compgen -W "${subcommands} ${tasks}" -- "${cur}") )
}
complete -F _devup_complete devup
"""

_ZSH_COMPLETION = r"""#compdef devup

_devup() {
    local -a subcommands flags dynamic_targets
    subcommands=(
        'list:List discovered tasks and groups'
        'ls:Alias for list'
        'run:Run one or more tasks or groups in parallel'
        'pick:Open interactive task picker'
        'init:Generate a starter devup.toml'
        'validate:Validate devup.toml and discovered targets'
        'doctor:Diagnose environment and installed runners'
        'completion:Generate shell autocompletion script'
    )
    flags=(
        '-h[Show help]'
        '--help[Show help]'
        '-V[Show version]'
        '--version[Show version]'
        '-C[Project root directory]:dir:_files -/'
        '--cwd[Project root directory]:dir:_files -/'
        '-w[Watch files and auto-restart]'
        '--watch[Watch files and auto-restart]'
        '--env-file[Load extra .env file]:file:_files'
        '--no-dotenv[Disable root .env loading]'
        '--no-port-check[Skip port availability checks]'
        '--strict-ports[Abort if port is already in use]'
        '--keep-going[Keep other services running on failure]'
        '--dry-run[Preview execution plan]'
        '-t[Prepend timestamps]'
        '--timestamps[Prepend timestamps]'
        '--no-prefix[Omit service prefixes]'
        '--json[JSON output]'
        '--force[Overwrite devup.toml]'
        '--online[Check PyPI for updates]'
        '--check-updates[Check PyPI for updates]'
        '--no-update-check[Skip update check]'
    )

    if (( CURRENT == 2 )); then
        _describe -t subcommands 'devup subcommand' subcommands
    fi

    if [[ "${words[CURRENT-1]}" == "completion" ]]; then
        _values 'shell' bash zsh fish powershell
        return
    fi

    dynamic_targets=(${(f)"$(devup list --json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print('\n'.join(list(d.get('groups',{}).keys()) + list(d.get('tasks',{}).keys())))" 2>/dev/null)"})
    _arguments -s $flags '*:target:compadd -a dynamic_targets'
}

_devup "$@"
"""

_FISH_COMPLETION = r"""# fish completion for devup
function __devup_targets
    devup list --json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print('\n'.join(list(d.get('groups',{}).keys()) + list(d.get('tasks',{}).keys())))" 2>/dev/null
end

complete -c devup -f
complete -c devup -n "__fish_use_subcommand" -a "list ls run pick init validate doctor completion"
complete -c devup -n "__fish_seen_subcommand_from completion" -a "bash zsh fish powershell"
complete -c devup -n "__fish_seen_subcommand_from run; or __fish_use_subcommand" -a "(__devup_targets)"
complete -c devup -s h -l help -d "Show help"
complete -c devup -s V -l version -d "Show version"
complete -c devup -s C -l cwd -r -d "Project root directory"
complete -c devup -s w -l watch -d "Watch files and auto-restart"
complete -c devup -l env-file -r -d "Load additional .env file"
complete -c devup -l no-dotenv -d "Disable root .env loading"
complete -c devup -l strict-ports -d "Abort if port is already in use"
complete -c devup -l no-port-check -d "Skip port availability checks"
complete -c devup -l keep-going -d "Keep other services running on failure"
complete -c devup -l dry-run -d "Preview execution plan"
complete -c devup -s t -l timestamps -d "Prepend timestamps"
complete -c devup -l no-prefix -d "Disable service prefixes"
complete -c devup -l json -d "Output JSON"
"""

_POWERSHELL_COMPLETION = r"""# PowerShell completion for devup
Register-ArgumentCompleter -Native -CommandName devup -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $subcommands = @('list', 'ls', 'run', 'pick', 'init', 'validate', 'doctor', 'completion')
    $flags = @('--help', '--version', '--cwd', '--watch', '--env-file', '--no-dotenv', '--strict-ports', '--no-port-check', '--keep-going', '--dry-run', '--timestamps', '--no-prefix', '--json', '--force', '--template', '--interactive', '--yes', '--online', '--check-updates', '--no-update-check')

    $tokens = $commandAst.CommandElements | ForEach-Object { $_.ToString() }
    if ($tokens.Count -ge 2 -and $tokens[1] -eq 'completion') {
        @('bash', 'zsh', 'fish', 'powershell') | Where-Object { $_ -like "$wordToComplete*" } | ForEach-Object {
            [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
        }
        return
    }

    $candidates = @()
    if ($wordToComplete -like '-*') {
        $candidates = $flags
    } else {
        $candidates = $subcommands
        try {
            $json = devup list --json 2>$null | ConvertFrom-Json
            if ($json.groups) {
                $candidates += $json.groups.PSObject.Properties.Name
            }
            if ($json.tasks) {
                $candidates += $json.tasks.PSObject.Properties.Name
            }
        } catch {}
    }

    $candidates | Sort-Object -Unique | Where-Object { $_ -like "$wordToComplete*" } | ForEach-Object {
        [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
    }
}
"""


def generate_completion(shell: str) -> str:
    """Return shell completion script for bash, zsh, fish, or powershell."""
    normalized = shell.strip().lower()
    mapping = {
        "bash": _BASH_COMPLETION,
        "zsh": _ZSH_COMPLETION,
        "fish": _FISH_COMPLETION,
        "powershell": _POWERSHELL_COMPLETION,
        "pwsh": _POWERSHELL_COMPLETION,
    }
    if normalized not in mapping:
        raise ValueError(
            f"Unsupported shell '{shell}'. Choose one of: {', '.join(SUPPORTED_SHELLS)}."
        )
    return mapping[normalized]
