# The Helm — rover cockpit (stub)

> Spoke of the [Merle Technical Guide](../../TechnicalGuide.md) — read the hub first for the machine roster, quick start, and cross-cutting conventions.
>
> **Covers:** the rover cockpit — `helm/` (the app) and `hands/` (the device service), the `craft/<id>/*` bus namespace. None of it is merged yet.
> **Runs on:** merle (hands), pearl (helm) — planned
> **Related:** epic #127; B0 is #203

Nothing to document yet. The design record is epic #127 and [the cockpit epic doc](../rover-cockpit-epic.md) — read Trap 6 before touching the rover: rover-hands **replaces** `ugv.service` rather than joining it, because Waveshare's `app.py` owns `/dev/ttyAMA0` and the USB camera exclusively. Until that cutover, the rover's only control path is the Waveshare UI documented in [Servers/Merle.md](../../Servers/Merle.md).

As B0 (#203) and its successors land, their sections accumulate here.
