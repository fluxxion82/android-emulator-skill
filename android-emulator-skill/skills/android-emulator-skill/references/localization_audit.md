# localization_audit.py

Audit `res/values*/strings.xml` for missing keys per locale and placeholder mismatches; optional source cross-reference.

Its flags and its one-line entry are in [SKILL.md](../SKILL.md); this file is the
detail: what each mode does, what it prints, and how it fails.

- An audit of nothing is a refusal (exit 1), never "No issues found": a
  `--res` with no `values/strings.xml` (often a path one level off, e.g.
  `app/src/main` for `app/src/main/res`), or a `--locale` code with no set
  there (no `values-<code>/` and no `values-<code>-*/` strings.xml; the
  refusal lists the codes it would accept). `--locale de` selects every
  German set, `values-de-land` and `values-de-port` alike; `--locale
  default` selects the default language's overrides. `--strict`'s exit 2
  stays for findings
- Only a locale qualifier makes a translation, read by Android's grammar
  (optional `mcc`/`mnc`, then `fr`, `fr-rCA` or `b+sr+Latn`; other
  qualifiers may follow). A `values-*` directory with no locale
  (`values-night`, `values-sw600dp`, `values-v31`, `values-land`), or a
  locale's qualified directory beside its plain one (`values-fr-night`
  beside `values-fr`), is an override: it is held to no key, but every
  string it supplies is checked against `values/` (findings labelled
  `default (night)`, `fr (night)`), and it is listed in `skipped` with the
  reason. A locale with no plain directory is audited per configuration,
  each against `values/` (`de (land)`, `de (port)`), never as their union
- Under `--json`, each audited set (`locales`) and each finding
  (`missing_keys`, `placeholder_mismatches`) names its set three ways:
  `locale`, the code `--locale` takes (`de`, `fr-rCA`, `b+sr+Latn`, or
  `default` for an override of the default language); `qualifiers`, the
  directory's other qualifiers (`land`, `night`) or null; and `label`, the
  display string text output uses (`de (land)`, `default (night)`)
- A `<plurals>` is compared item by item, never joined: a language uses its
  own plural categories (Serbian `one/few/other`, Arabic all six), so a
  translation with more items than `values/` is no finding. Each item is
  read as the arguments it formats (`%d` reads argument 1 as `%1$d` does;
  `%d %d` reads two). It may read only arguments some default item reads,
  must read every argument all default items read, and its `other` item
  (a missing one reads as empty) every argument the default's `other`
  reads. A plural finding names its item in `quantity` (`few`; null for a
  string) and shows placeholders by argument (`%1$d`)
