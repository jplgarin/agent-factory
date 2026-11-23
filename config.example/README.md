# config.example

Fully synthetic example of the private configuration that Agent Factory loads from `AF_CONFIG_DIR`.
Every name, address and preference here is invented. Addresses use reserved example domains
(RFC 2606), and a test fails if any other domain appears.

Your real configuration lives in a separate private repository. Point `AF_CONFIG_DIR_HOST` in `.env`
at it; Docker Compose mounts it read-only at `/config` in the gateway and workers.

```text
config.example/
├── mailboxes.yaml          # the three mailboxes and the space each belongs to
└── spaces/
    ├── personal/
    │   └── profile.yaml    # who the agents work for, in this space
    └── synaption/
        └── profile.yaml
```

The file formats are provisional. Loaders and schema validation arrive with the milestones that use
them: profiles and preferences in M3 (context builder), mailboxes in M5a (email agent).
