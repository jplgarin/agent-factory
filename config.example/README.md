# config.example

Fully synthetic example of the private configuration that Agent Factory loads from `AF_CONFIG_DIR`.
Every name, address and preference here is invented. Addresses use reserved example domains
(RFC 2606), and a test fails if any other domain appears.

Your real configuration lives in a separate private repository. Point `AF_CONFIG_DIR_HOST` in `.env`
at it; Docker Compose mounts it read-only at `/config` in the gateway and workers.

```text
config.example/
├── mailboxes.yaml              # the three mailboxes and the space each belongs to
└── spaces/
    ├── personal/
    │   ├── profile.yaml        # who the agents work for, in this space
    │   └── preferences.yaml    # how they should do their work
    └── synaption/
        ├── profile.yaml
        └── preferences.yaml
```

## Profile and preferences

Both files are read every time an agent's context is built and are never copied into the database.
The audit log records the SHA-256 of each file, so every run states which version it used.
Unknown keys are rejected. `apps/shared/space_config.py` loads them; the schemas are
`SpaceProfile` and `Preferences` in `core/context/documents.py`.

`profile.yaml`: stable facts about you in this space.

| Key | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Your name. |
| `role` | no | Your role in this space. |
| `languages` | yes | Language tags you write in, for example `[en, es]`. |
| `timezone` | yes | IANA time zone, for example `Europe/Madrid`. |
| `facts` | no | Up to 50 one-line facts the agents should always know. |

`preferences.yaml`: how you want things done.

| Key | Required | Meaning |
| --- | --- | --- |
| `style` | no | Up to 30 named settings, for example `reply_tone: formal`. |
| `rules` | no | Up to 50 one-line standing rules. |

An agent's system prompt is also a file in this directory, named by `system_prompt_path` in the
agent's spec. The first prompts arrive with the first agents.

The `mailboxes.yaml` format is provisional; its loader and validation arrive in M5a.
