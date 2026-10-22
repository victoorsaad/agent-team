# agent-team

**English** · [Português](#português)

A small company's worth of roles, run by [Claude Code](https://docs.claude.com/en/docs/claude-code) agents. They hand
work to each other through a shared mailbox, review each other's code, and bring the humans only the decisions that
are actually theirs. A 3D "office" shows who is working on what.

![The office, playing the scripted demo](docs/office.png)

*The office playing `examples/tea-shop/replay.json`, a scripted conversation of a made-up tea shop.*

## What it is

- **Roles are configuration.** `team.json` lists the agents (id, name, room, prompt file, allowed tools), the humans,
  who approves what, the orchestrator and the reviewer, plus routines and triggers. The example is a fictional
  two-person online tea shop, *Fern & Kettle*, with an orchestrator (Ops), a dev, a reviewer, marketing, support and
  a weekly retro.
- **A mailbox exposed as an MCP server** (`team`): `ask`, `assign`, `reply`, `inbox`, `thread`, `team_log`,
  `post_update`, `share`, `request_approval`, `commit`/`commit_done`/`commit_failed` (promises), `remember`,
  `teammates`; `decide` for whoever relays a human's answer; optionally `propose_email`/`outbox`, `review` and
  `wait_for_pr`. SQLite, standard library only.
- **An optional orchestrator** (see below): one agent is the humans' only door, an authority ladder says what the team
  decides alone, approvals are transactions with a receipt, promises are registered, and the day ends in one digest.
- **A dispatcher** that wakes an agent only when it has mail (`claude -p`), so nobody polls and a run takes minutes.
  Conversations a human started go first. Caps per day, per run and on self-started work; pauses on usage limits.
- **Routines and triggers**: scheduled tasks ("Monday 10:00, marketing drafts the newsletter") and "new lines in this
  feed or this command's output become a task" (changelogs, error logs, new support tickets).
- **Human approval**: the rules go into every agent's prompt; `request_approval` reaches the humans; outgoing e-mail is
  enforced by a separate mailer that verifies the approval out of band.
- **Optional integrations, off by default**: Discord (humans talk to the team in a channel, one thread per
  conversation), e-mail over SMTP, GitHub PR review queue and merge gate.
- **The office** (Three.js): agents walk to their desks when they have work, envelopes fly between them, they go back
  to the kitchen when done. A "Done today" tab lists what got done and why (asked by a human, routine, initiative).
  A **replay** mode plays a scripted conversation for demos.

## The orchestrator (optional)

Without these keys a team works as in v0.1: a message without a name goes to `orchestrator` (the first role by
default), an answer inside a chat thread goes to whoever spoke last there, and `share` reaches the humans at once.
Each feature below is off until its key is in `team.json`; the example turns them all on.

| Key | What it does |
|---|---|
| `orchestrator` + `"single_door": true` | Every human message without a name goes to the orchestrator, inside a chat thread too. It routes the work, resolves the humans' answers against what's pending in that conversation (asking in one line when "approve" could mean two things), records them with `decide`, and closes every request with one receipt: done + proof, couldn't + why, or promised by when. Agents answer whoever asked them, not the humans. |
| `authority` | `levels` (0: the agent, 1: the orchestrator or a lead, 2: a human, not urgent, 3: a human, now; names and examples are yours), `rules` (the higher level wins in doubt, precedents, a trust ladder) and `locks` (what nobody changes without a human, the retro included). Rendered into every agent's instructions. `remember` with `precedent: true` turns a human's answer into a standing rule. |
| `decision_kinds` | The kinds `request_approval` accepts (default `pr`, `spend`, `legal`, `other`). Every request opens a row in `decisions`; `pr` needs the PR number, and the merge gate waits for it. |
| `promises` | `commit(what, due, depends)` registers "I'll do it later"; the dispatcher wakes the agent in the same conversation when `depends` closes (`pr:<n>` merged, `decision:<id>` decided) or at `due`, and tells the orchestrator once when it's `chase_after_min` (120) overdue. With `require` (default: on with a single door) the tools refuse "I'll let you know when…" without a promise. |
| `digest` | `{"at": "19:00", "days": [...]}`. `share` gets an `urgent` flag; anything else waits for one daily message assembled by code from the tables (done, found, decided, waiting for you, open promises, cost) and posted as the orchestrator. `agent-team digest` prints today's. |
| `merge_gate` | With `TEAM_GITHUB=1` and a `reviewer`: the reviewer only reviews (`review` ok / ok + needs a human / block), and the dispatcher merges an ok PR when CI is green on that same commit, no decision for it is pending or was rejected since the review, `sensitive_paths` (regexes) have a human's yes, and `deploy_workflow` (optional) isn't running; one a minute, with a receipt in the human's thread. |
| `heartbeat.when` | `"backlog"`: the heartbeat runs only when a PR has been open for more than a day or a new issue appeared (needs GitHub); `{why}` in its prompt says which. Default `"always"`. |
| `retro` | `{"agent", "days": [0], "at": "08:30", "max_changes": 3, "notes_dir"}`: a weekly task with the data to read (the mailbox, read-only) and what to measure (receipts, promises, approvals, cost per result), at most N changes by PR, each waiting for a human, never touching the `locks`. |

The humans can also answer from the command line: `agent-team decisions`, `agent-team decide <id> approve|reject`,
`agent-team promises`. With Discord, a plain "approve" in a thread where a PR (or any decision) and emails both wait
approves no email: the bot says it's ambiguous and the orchestrator asks; "approve the emails" or the email numbers do
it, and bare numbers count only when they are pending email ids.

## Architecture

```mermaid
flowchart LR
  subgraph Humans
    H1[CLI: agent-team send / approve]
    H2[Own Claude Code session<br/>MCP team --as human]
    H3[Discord channel<br/>optional]
  end
  subgraph Server
    DB[(SQLite mailbox<br/>messages · runs · outbox<br/>decisions · promises · done)]
    MCP[MCP server `team`<br/>one per agent run]
    D[Dispatcher<br/>caps · routines · triggers · PR queue<br/>merge gate · promises · digest · retro]
    O[Office<br/>HTTP + Three.js]
    M[Mailer<br/>optional, own user]
    B[Discord bot<br/>optional, own user]
  end
  C1[claude -p<br/>ops]:::agent
  C2[claude -p<br/>dev]:::agent
  C3[claude -p<br/>...]:::agent
  H1 --> DB
  H2 --> MCP
  H3 <--> B --> DB
  D -- new mail --> C1 & C2 & C3
  C1 & C2 & C3 <--> MCP <--> DB
  D <--> DB
  O --> DB
  M -- verified approvals --> SMTP[(SMTP)]
  M --> DB
  classDef agent fill:#eef,stroke:#88a
```

A message to an agent sits in the mailbox until the dispatcher starts `claude -p` for that agent with its role
prompt, the team playbook, the approval rules and the team memory. The agent opens its inbox through the `team` MCP
tools, works (in your repository, with the tools its role allows), asks or assigns teammates and replies. Each run
handles one conversation and ends; answers wake whoever is waiting.

| File | What it does |
|---|---|
| `agent_team/config.py` | settings (env / `.env`) and `team.json` loading and validation |
| `agent_team/store.py` | the mailbox (SQLite), file checks, approvals, decisions, promises, the done ledger |
| `agent_team/mcp_server.py` | the `team` MCP server (stdio JSON-RPC) |
| `agent_team/dispatcher.py` | wakes agents; heartbeat, routines, triggers, caps, GitHub PR queue, merge gate, promises, digest, retro |
| `agent_team/office.py`, `office.html` | the office (HTTP + Three.js) and its access rules |
| `agent_team/replay.py` | plays a scripted conversation into a throwaway database |
| `agent_team/mailer.py` | optional: sends human-approved e-mail over SMTP |
| `agent_team/discord_bot.py`, `notify.py` | optional: Discord bot and mirror |
| `examples/tea-shop/` | the fictional example team: `team.json`, prompts, `TEAM.md`, `replay.json` |
| `deploy/` | example systemd install (separate OS users per service) |

## Run it locally

Requirements: Python 3.9+ (standard library only), and [Claude Code](https://docs.claude.com/en/docs/claude-code)
for real runs. `discord.py` only if you enable the Discord bot.

```bash
git clone <this repository> agent-team && cd agent-team
cp .env.example .env              # TEAM_DRY_RUN=1: no Claude calls yet

python3 -m agent_team check       # validates examples/tea-shop/team.json
python3 -m unittest discover -s tests   # smoke tests, no network

# 1. The office with the scripted demo (nothing real): http://127.0.0.1:8130
python3 -m agent_team office --replay examples/tea-shop/replay.json

# 2. The loop in dry-run: a stub answers instead of Claude
python3 -m agent_team send "Can we add an autumn bundle by Friday?"
python3 -m agent_team dispatcher            # Ctrl-C to stop
python3 -m agent_team log                   # what happened
python3 -m agent_team office                # live office on the real mailbox
```

For real runs: log in to Claude Code (`claude setup-token` or an API key), point `TEAM_WORKDIR` to your project, set
`TEAM_DRY_RUN=0`, and start the dispatcher. Talk to the team with `agent-team send`, from your own Claude Code
session (`python3 -m agent_team mcp-config --as ana` prints the MCP entry; then "ask ops …", "what did the team do
today?"), or from Discord.

**Your own team:** copy `examples/tea-shop/`, edit `team.json` (roles, humans, approval rules, routines, triggers)
and the prompts, set `TEAM_CONFIG`, and run `agent-team check`. `agent-team prompt <agent>` shows exactly what an
agent is told.

## Security

Read [SECURITY.md](SECURITY.md). In short:

- **Human approval.** Approval rules (and the authority levels) are part of every agent's instructions;
  `request_approval` opens a decision for the humans. E-mail is enforced: the mailer sends only what a human approved,
  verified with an HMAC (`agent-team approve`) or against Discord's API, and only if the email is unchanged.
- **The merge gate** is code: the dispatcher never merges around a pending or rejected decision, a red or unfinished
  CI, or a sensitive path without a human's yes. It guards against the team's own mistakes, not against a compromised
  agent: an agent whose GitHub token can merge can still merge by hand. Take merge rights away from the agents' token
  (branch protection) to make it a boundary; see SECURITY.md.
- **Sandbox.** Run agents as their own OS user, in a container or VM, with only the repository they work on.
  `TEAM_PERMISSION_MODE=default` + per-role `allowed_tools` by default; `bypassPermissions` only inside a sandbox.
- **Secrets.** Never in `team.json`, prompts or the repo: `.env` (ignored by git) or per-service env files readable
  only by that service's user. The mailbox is not an auth boundary; agents must not be able to read the mailer's or
  the bot's secrets. Attachments are checked (no dotfiles, keys or env files).
- **Budgets and a stop button.** Parallel runs, daily caps, a timeout per run, `agent-team stop`.

## Limitations

- Built for **Claude Code** (`claude -p`, stream-json output, MCP). Other agent CLIs would need a new runner.
- **One machine**: SQLite, threads, no queue. Fine for a small team; not a distributed system.
- **Prompt injection is not solved.** Agents read untrusted text; the defenses are least privilege and human approval
  for side effects, not detection.
- **Soft approvals.** Except e-mail and the merges the gate does, approvals are instructions the agent follows, not
  enforced gates; `decide` checks that a human spoke in that conversation, but the mailbox is writable by agents.
  Enforce new side effects the way the mailer does.
- The orchestrator's judgment (routing, "approve which?", receipts) is a prompt; the code only gives it the pending
  items, refuses the obvious mistakes and keeps the tables. The trust ladder is counted by the retro, not by code.
- The dry run checks the plumbing, not the agents' judgment. Quality depends on your prompts and playbook.
- The office loads Three.js and fonts from public CDNs; it needs a browser with WebGL.
- GitHub integration needs `gh` logged in and only knows PRs and checks. The Discord bot is lightly tested.
- Early, small codebase (v0.1). Expect breaking changes.

## License

MIT, see [LICENSE](LICENSE).

---

# Português

[English](#agent-team) · **Português**

Os papéis de uma pequena empresa, ocupados por agentes do [Claude Code](https://docs.claude.com/en/docs/claude-code).
Eles passam trabalho entre si por uma caixa de correio compartilhada, revisam o código um do outro e só levam às
pessoas as decisões que são de fato delas. Um "escritório" em 3D mostra quem está trabalhando em quê.

## O que é

- **Papéis são configuração.** O `team.json` lista os agentes (id, nome, sala, arquivo de prompt, ferramentas
  permitidas), as pessoas, quem aprova o quê, o orquestrador e o revisor, além de rotinas e gatilhos. O exemplo é uma
  loja online de chá fictícia, a *Fern & Kettle*, com um orquestrador (Ops), dev, revisor, marketing, atendimento e
  uma retro semanal.
- **Uma caixa de correio exposta como servidor MCP** (`team`): `ask`, `assign`, `reply`, `inbox`, `thread`,
  `team_log`, `post_update`, `share`, `request_approval`, `commit`/`commit_done`/`commit_failed` (promessas),
  `remember`, `teammates`; `decide` para quem repassa a resposta de uma pessoa; opcionalmente
  `propose_email`/`outbox`, `review` e `wait_for_pr`. SQLite, só biblioteca padrão.
- **Um orquestrador opcional** (veja abaixo): um agente é a única porta das pessoas, uma escada de alçada diz o que o
  time decide sozinho, aprovações viram transações com recibo, promessas ficam registradas e o dia termina num resumo.
- **Um dispatcher** que acorda um agente só quando ele tem mensagem (`claude -p`): ninguém faz polling e cada execução
  leva minutos. Conversas iniciadas por uma pessoa passam na frente. Limites por dia, por execução e para trabalho
  que o time inicia sozinho; pausa quando bate o limite de uso.
- **Rotinas e gatilhos**: tarefas agendadas ("segunda 10h, marketing rascunha a newsletter") e "linhas novas neste
  feed ou na saída deste comando viram tarefa" (changelogs, logs de erro, tickets novos de atendimento).
- **Aprovação humana**: as regras entram no prompt de todo agente; `request_approval` chega às pessoas; o e-mail que
  sai é garantido por um mailer separado que confere a aprovação por fora.
- **Integrações opcionais, desligadas por padrão**: Discord (as pessoas falam com o time num canal, uma thread por
  conversa), e-mail por SMTP, fila de revisão de PRs e portão de merge no GitHub.
- **O escritório** (Three.js): os agentes vão até a mesa quando têm trabalho, envelopes voam entre eles, e voltam para
  a copa quando terminam. A aba "Feito hoje" lista o que foi feito e por quê (a pedido, rotina, iniciativa). O modo
  **replay** reproduz uma conversa roteirizada para demonstração.

## O orquestrador (opcional)

Sem estas chaves, o time funciona como na v0.1: mensagem sem nome vai para o `orchestrator` (por padrão, o primeiro
papel), resposta dentro de uma thread vai para quem falou por último ali, e o `share` chega às pessoas na hora. Cada
recurso abaixo fica desligado até a chave estar no `team.json`; o exemplo liga todos.

| Chave | O que faz |
|---|---|
| `orchestrator` + `"single_door": true` | Toda mensagem de uma pessoa sem nome vai para o orquestrador, inclusive dentro de thread. Ele distribui o trabalho, resolve a resposta da pessoa contra o que está pendente naquela conversa (pergunta numa linha quando "aprovo" pode ser duas coisas), registra com `decide` e fecha todo pedido com um recibo: feito + prova, não deu + motivo, ou prometido até quando. Os agentes respondem a quem pediu, não às pessoas. |
| `authority` | `levels` (0: o agente, 1: o orquestrador ou um líder, 2: uma pessoa, sem urgência, 3: uma pessoa, na hora; nomes e exemplos são seus), `rules` (na dúvida vale o nível mais alto, precedentes, escada de confiança) e `locks` (o que ninguém muda sem uma pessoa, nem a retro). Entra nas instruções de todo agente. `remember` com `precedent: true` transforma a resposta de uma pessoa em regra. |
| `decision_kinds` | Os tipos que o `request_approval` aceita (padrão `pr`, `spend`, `legal`, `other`). Todo pedido abre uma linha em `decisions`; `pr` exige o número do PR, e o portão de merge espera por ela. |
| `promises` | `commit(what, due, depends)` registra o "faço depois"; o dispatcher acorda o agente na mesma conversa quando `depends` fecha (`pr:<n>` mergeado, `decision:<id>` decidida) ou no `due`, e avisa o orquestrador uma vez quando passa `chase_after_min` (120) do prazo. Com `require` (padrão: ligado com porta única), as ferramentas recusam "te aviso quando…" sem promessa. |
| `digest` | `{"at": "19:00", "days": [...]}`. O `share` ganha `urgent`; o resto espera uma mensagem por dia montada por código a partir das tabelas (feito, achados, decidido, esperando vocês, promessas abertas, custo) e postada como o orquestrador. `agent-team digest` mostra o de hoje. |
| `merge_gate` | Com `TEAM_GITHUB=1` e um `reviewer`: o revisor só revisa (`review` ok / ok + precisa de pessoa / bloqueia), e o dispatcher mergeia um PR ok quando o CI está verde naquele mesmo commit, nenhuma decisão dele está pendente ou foi recusada depois da revisão, os `sensitive_paths` (regex) têm o sim de uma pessoa e o `deploy_workflow` (opcional) não está rodando; um por minuto, com recibo na thread da pessoa. |
| `heartbeat.when` | `"backlog"`: o heartbeat só roda quando há PR aberto há mais de um dia ou issue nova (precisa do GitHub); `{why}` no prompt diz qual. Padrão `"always"`. |
| `retro` | `{"agent", "days": [0], "at": "08:30", "max_changes": 3, "notes_dir"}`: uma tarefa semanal com os dados a ler (a caixa, só leitura) e o que medir (recibos, promessas, aprovações, custo por resultado), no máximo N mudanças por PR, cada uma esperando uma pessoa, sem nunca mexer nos `locks`. |

As pessoas também respondem pela linha de comando: `agent-team decisions`, `agent-team decide <id> approve|reject`,
`agent-team promises`. No Discord, um "aprovo" puro numa thread onde esperam um PR (ou qualquer decisão) e e-mails não
aprova nenhum e-mail: o bot avisa que é ambíguo e o orquestrador pergunta; "aprovo os emails" ou os números dos e-mails
aprovam, e números soltos só valem quando são ids de e-mails pendentes.

## Arquitetura

O diagrama está na seção em inglês, acima. Uma mensagem para um agente fica na caixa até o dispatcher rodar
`claude -p` para ele, com o prompt do papel, o manual do time (`TEAM.md`), as regras de aprovação e a memória do
time. O agente abre a caixa pelas ferramentas MCP `team`, trabalha (no seu repositório, com as ferramentas que o
papel permite), pergunta ou passa tarefas aos colegas e responde. Cada execução trata uma conversa e termina; a
resposta acorda quem estava esperando. A tabela de arquivos também está acima.

## Como rodar local

Requisitos: Python 3.9+ (só biblioteca padrão) e, para execuções de verdade, o Claude Code. `discord.py` só se ligar
o bot do Discord.

```bash
git clone <este repositório> agent-team && cd agent-team
cp .env.example .env              # TEAM_DRY_RUN=1: ainda sem chamar o Claude

python3 -m agent_team check       # valida examples/tea-shop/team.json
python3 -m unittest discover -s tests   # testes de fumaça, sem rede

# 1. O escritório com a demonstração roteirizada (nada real): http://127.0.0.1:8130
python3 -m agent_team office --replay examples/tea-shop/replay.json

# 2. O ciclo em modo seco: um stub responde no lugar do Claude
python3 -m agent_team send "Dá para lançar um kit de outono até sexta?"
python3 -m agent_team dispatcher            # Ctrl-C para parar
python3 -m agent_team log                   # o que aconteceu
python3 -m agent_team office                # escritório ao vivo, na caixa real
```

Para valer: faça login no Claude Code (`claude setup-token` ou chave de API), aponte `TEAM_WORKDIR` para o seu
projeto, `TEAM_DRY_RUN=0` e suba o dispatcher. Fale com o time por `agent-team send`, pela sua própria sessão do
Claude Code (`python3 -m agent_team mcp-config --as ana` imprime a entrada do MCP; depois "pergunte ao ops …") ou pelo
Discord. Para usar o time
em português, `"language": "pt"` no `team.json` (mensagens às pessoas e a interface do escritório).

**O seu time:** copie `examples/tea-shop/`, edite o `team.json` e os prompts, aponte `TEAM_CONFIG` e rode
`agent-team check`. `agent-team prompt <agente>` mostra exatamente o que o agente recebe.

## Segurança

Leia o [SECURITY.md](SECURITY.md). Em resumo:

- **Aprovação humana.** As regras (e os níveis de alçada) fazem parte das instruções de todo agente;
  `request_approval` abre uma decisão para as pessoas. No e-mail é obrigatório: o mailer só envia o que uma pessoa
  aprovou, conferido por HMAC (`agent-team approve`) ou pela API do Discord, e só se o e-mail não mudou.
- **O portão de merge** é código: o dispatcher nunca mergeia passando por cima de uma decisão pendente ou recusada, de
  um CI vermelho ou inacabado, ou de um caminho sensível sem o sim de uma pessoa. Ele protege contra os erros do
  próprio time, não contra um agente comprometido: um agente cujo token do GitHub pode mergear ainda consegue mergear
  na mão. Para virar fronteira, tire o direito de merge do token dos agentes (branch protection); veja o SECURITY.md.
- **Sandbox.** Rode os agentes com usuário próprio do sistema, em container ou VM, só com o repositório em que
  trabalham. Padrão: `TEAM_PERMISSION_MODE=default` + `allowed_tools` por papel; `bypassPermissions` só dentro de sandbox.
- **Segredos.** Nunca no `team.json`, nos prompts ou no repositório: `.env` (ignorado pelo git) ou arquivos de
  ambiente por serviço, legíveis só pelo usuário daquele serviço. A caixa de correio não é fronteira de segurança;
  os agentes não podem conseguir ler os segredos do mailer e do bot. Anexos são conferidos (sem dotfiles, chaves ou .env).
- **Limites e botão de parar.** Execuções paralelas, limites diários, timeout por execução, `agent-team stop`.

## Limitações

- Feito para o **Claude Code** (`claude -p`, saída stream-json, MCP). Outros CLIs de agente pediriam outro executor.
- **Uma máquina só**: SQLite, threads, sem fila. Serve para um time pequeno; não é sistema distribuído.
- **Prompt injection não está resolvido.** Os agentes leem texto não confiável; a defesa é menor privilégio e
  aprovação humana para efeitos externos, não detecção.
- **Aprovações "macias".** Fora o e-mail e os merges que o portão faz, aprovação é instrução que o agente segue, não
  trava obrigatória; o `decide` confere que uma pessoa falou naquela conversa, mas a caixa aceita escrita dos agentes.
  Novos efeitos externos devem ser garantidos do jeito que o mailer faz.
- O julgamento do orquestrador (roteamento, "aprova o quê?", recibos) é prompt; o código só entrega o que está
  pendente, recusa os erros óbvios e mantém as tabelas. A escada de confiança é contada pela retro, não por código.
- O modo seco testa o encanamento, não o julgamento dos agentes. A qualidade depende dos prompts e do manual.
- O escritório carrega Three.js e fontes de CDNs públicas; precisa de navegador com WebGL.
- A integração com GitHub precisa do `gh` logado e só conhece PRs e checks. O bot do Discord foi pouco testado.
- Código pequeno e inicial (v0.1). Espere mudanças incompatíveis.

## Licença

MIT, veja [LICENSE](LICENSE).
