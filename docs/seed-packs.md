# Seed packs

A seed pack is the content a new, empty database starts with: brands,
personas, house-style guidelines, and an optional rolling growth mission.
Brand-specific content lives here, as data, not in code.

`BRANDMAN_SEED_PACK` chooses the pack:

- `demo` (default): two sample brands.
- `none`: start empty (what `brandman init` uses).
- a path to a JSON file;
- a name registered by an installed package under the `brandman.seed_packs`
  entry-point group (the value may be a path, a dict, or a callable returning
  either).

Seeding happens once, when the database has no brands. Guidelines from the
pack are created only if the brand has none for that content type and
channel, so later edits are never overwritten.

```json
{
  "name": "acme",
  "claim_units": ["credits", "seats"],
  "brands": [{
    "slug": "acme",
    "name": "Acme",
    "mission": "Help teams pick the right plan.",
    "voice": "Plain, specific, friendly.",
    "compliance_rules": "Verify prices and dates before publishing.",
    "personas": [{"name": "Buyers", "audience": "Team leads comparing plans", "angles": ["save money"]}],
    "guidelines": [{
      "content_type": "newsletter", "channel": "beehiiv",
      "name": "Acme newsletter house style",
      "instructions": "Open with 'Hey,'. One thesis per issue…",
      "rules": {
        "minimum_words": 600,
        "required_opening": "Hey,",
        "required_signoff": "— Acme",
        "approved_tool_backlinks": ["https://acme.example/pricing"],
        "topic_backlinks": [{"topic": "pricing", "urls": ["https://acme.example/pricing"]}],
        "operator_checklist": ["one_thesis", "bottom_line"]
      }
    }],
    "growth_mission": {
      "name": "Acme 30-day growth",
      "timezone": "America/New_York",
      "goals": {"x_followers": [0, 100], "active_beehiiv_subscribers": [0, 25]}
    }
  }]
}
```

A growth mission rolls over automatically: when the window ends, a new one
starts from the last real observation with the same growth step.
