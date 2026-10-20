# Host-session experiments (2026-10-20)

Run from an unsandboxed shell on the host (Ubuntu 22.04.5, WSL2 kernel 6.18.33.2, uid 1000) with Codex CLI 0.163.0, Claude Code 2.1.304, srt 0.0.81, bubblewrap 0.9.0, socat, ripgrep 15.2.0, Node 22.23.2. Raw outputs below are verbatim; the temporary directories (`/tmp/ccsbx.QJ5A`, `/tmp/cc-audit-ws`, `/tmp/ccdep.*`) were removed afterwards.

## E1 Codex sandbox on the host

```
$ cd /tmp && codex sandbox --permission-profile p -c 'permissions.p.extends=":read-only"' -- /usr/bin/true; echo rc=$?
rc=0
$ codex sandbox -- sh -c 'ls -ld /tmp/codex-daemon-1000; grep -E " /tmp( |/)" /proc/self/mountinfo | cut -d" " -f5,6,9,10,11; id; echo HOME=$HOME; echo TMPDIR=$TMPDIR'
d--------- 2 user user 40 Oct 20 15:43 /tmp/codex-daemon-1000
/tmp/.X11-unix ro,nosuid,nodev,relatime tmpfs none rw
/tmp/codex-daemon-1000 ro,nosuid,nodev,relatime tmpfs rw,mode=000,uid=1000,gid=1000
uid=1000(user) gid=1000(user) groups=1000(user),65534(nogroup)
HOME=$HOME
TMPDIR=/tmp/.ctx-mode-9m5S5P
rc=0
$ ls -ld /tmp/codex-daemon-1000        # on the host
drwx------ 2 user user 4096 Oct 20 09:44 /tmp/codex-daemon-1000
```

## E2 Nested Codex

```
$ codex sandbox -- codex sandbox -- /usr/bin/true
WARNING: proceeding, even though we could not create PATH aliases: Read-only file system (os error 30)
error building bubblewrap command: app-server socket directory must be a user-owned directory with mode 0700
rc=1
$ codex sandbox --permission-profile p -c 'permissions.p.extends=":workspace"' -- codex sandbox --permission-profile q -c 'permissions.q.extends=":read-only"' -- /usr/bin/true
(same error)
```

## E3 srt standalone and cross-vendor nesting

srt.json: `{"network":{"allowedDomains":["api.github.com"],"deniedDomains":[],"allowLocalBinding":false},"filesystem":{"allowWrite":["/tmp/ccsbx.QJ5A/scratch"],"denyWrite":[],"denyRead":["~/.ssh","~/.aws","~/.config/gh"]}}`

```
--- srt basic (cwd = clone)
write scratch: WRITABLE
write clone: denied
write HOME: denied
net example.com: curl: (56) Received HTTP code 403 from proxy after CONNECT
net api.github.com: 200
docker sock: curl: (7) Couldn't connect to server
codex-daemon dir: drwx------ 2 user user 4096 Oct 20 09:44 /tmp/codex-daemon-1000
rc=0
--- codex sandbox inside srt
thread 'main' (28) panicked at linux-sandbox/src/linux_run_main.rs:1362:17:
failed to resolve synthetic mount registry temp directory /tmp/claude: No such file or directory (os error 2)
rc=101
--- srt inside codex sandbox
Error: listen EPERM: operation not permitted /tmp/.ctx-mode-WzWuo1/srt-mux-2-0.sock
rc=1
--- srt inside srt
Error: listen EPERM: operation not permitted /tmp/claude/srt-mux-2-0.sock
rc=1
```

## E4 Claude review sandbox (one claude-haiku-5-5 call)

Settings passed: `{"sandbox": {"enabled": true, "failIfUnavailable": true, "allowUnsandboxedCommands": false, "filesystem": {"allowWrite": ["/tmp/ccsbx.QJ5A/clone", "/tmp/ccsbx.QJ5A/scratch"]}, "network": {"allowedDomains": ["github.com", "api.github.com", "bitbucket.org", "api.bitbucket.org"]}}}` (output of `stage_permissions.review_sandbox_settings`).

```
$ env -u CLAUDECODE TMPDIR=$B/scratch claude -p "Run exactly this one Bash command and reply with only its raw output verbatim, no commentary: sh $B/scratch/probe.sh" --model claude-haiku-5-5 --permission-mode default --allowedTools "Bash(sh $B/scratch/probe.sh:*)" --settings "$SET" --output-format json
is_error False
denials []
placeholders:
drwx------  2 user user 4096 Oct 20 15:45 .cc-writes
crw-rw-rw-  1 nobody  nogroup 1, 3 Oct 20 09:43 agents
crw-rw-rw-  1 nobody  nogroup 1, 3 Oct 20 09:43 commands
crw-rw-rw-  1 nobody  nogroup 1, 3 Oct 20 09:43 hooks
crw-rw-rw-  1 nobody  nogroup 1, 3 Oct 20 09:43 launch.json
crw-rw-rw- 1 nobody nogroup 1, 3 Oct 20 09:43 .bashrc
crw-rw-rw- 1 nobody nogroup 1, 3 Oct 20 09:43 .gitconfig
crw-rw-rw- 1 nobody nogroup 1, 3 Oct 20 09:43 .mcp.json
copytree-like read .claude/settings.json: EACCES
ssh read: READABLE
claude.json read: READABLE
codex auth read: READABLE
gh hosts read: READABLE
write TMPDIR: WRITABLE
write clone: WRITABLE
write HOME: denied
net example.com: curl: (56) Received HTTP code 403 from proxy after CONNECT
net api.github.com: 200
nested codex sandbox: bwrap: No permissions to creating new namespace, likely because the kernel does not allow non-privileged user namespaces.
nested srt: Error: listen EPERM: operation not permitted /tmp/ccsbx.QJ5A/scratch/claude-1000/srt-mux-49-0.sock
codex-daemon dir: drwx------ 2 user user 4096 Oct 20 09:44 /tmp/codex-daemon-1000
<sandbox_violations>
deny network-outbound example.com:443 (user denied)
</sandbox_violations>
```

## E5 dependency_setup.prepare on the host

Throwaway repo: `package.json` with `is-odd@3.0.1`, `package-lock.json` generated with `npm install --package-lock-only --ignore-scripts`, one commit.

```
=== prepare executor=codex
status prepared
 manager npm status prepared
  cmd npm --version exit 0                       sandbox_argv0 codex
  cmd npm ci --ignore-scripts --no-audit --no-fund --cache ~/.local/state/code-cycle-toolkit/preparation/cache/9094a35e... exit 0
  stderr tail: ... (node:2) [UNDICI-EHPA] Warning: EnvHttpProxyAgent is experimental ...
=== prepare executor=claude
status prepared
 manager npm status prepared
  cmd npm --version exit 0                       sandbox_argv0 $HOME/.nvm/versions/node/v22.23.2/bin/srt
  cmd npm ci --ignore-scripts --no-audit --no-fund --cache ~/.local/state/code-cycle-toolkit/preparation/cache/9094a35e... exit 0
```

## E7 negative controls and credential reads

```
--- codex sandbox (default read-only), cwd /tmp/cc-audit-ws
read ssh dir: READABLE
read codex auth: READABLE
read claude.json: READABLE
write HOME: denied
write cwd: denied
write /tmp: denied
net: curl: (6) Could not resolve host: example.com
unix socket docker: curl: (7) Couldn't connect to server
write git dir: mkdir: cannot create directory '.git': Read-only file system
--- credential content probe, codex sandbox (default)
ssh entries visible: 13
ssh file content: READABLE ($HOME/.ssh/<key>)
gh hosts content: READABLE
codex auth content: READABLE
claude creds content: READABLE   ($HOME/.claude/.credentials.json)
--- codex with unreadable rule: permissions.p.filesystem={"$HOME/.ssh"="none","$HOME/.config/gh"="none"}
ssh entries visible: 0
gh hosts content: denied
codex auth content: READABLE
claude creds content: READABLE
--- srt with denyRead ~/.ssh ~/.aws ~/.config/gh
ssh entries visible: 0
gh hosts content: denied
codex auth content: READABLE
claude creds content: READABLE
--- host
$ sysctl -n user.max_user_namespaces
79955
$ claude sandbox status
{"statusVersion":3,...,"supported":true,"enabled":false,"enabledSource":"off",...}
```
