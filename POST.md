*This is a submission for the [Hacktoberfest Open-Source AI Challenge Week 3: Slow and Steady](https://dev.to/challenges/hacktoberfest-week3-2026-10-19)*

![Eventually cover](https://raw.githubusercontent.com/BhushanVidhate-27/eventually/main/assets/cover.png)

## What I Built

Eventually is a small Python CLI that tidies a folder without opening a single file. You point it at `~/Downloads`; it reads the names of the loose files, asks a local open-weight model where each one belongs, prints a plan, and waits. `--apply` moves them. `--undo` puts every file back.

It is not a file manager. It has one job: close the open loop that is my Downloads folder.

The model is Llama 3.2 1B, running through Ollama on my laptop. It chooses among 11 folders: Documents, Spreadsheets, Images, Screenshots, Installers, Archives, Audio, Video, Code, Receipts, and To Sort. The filenames never leave the machine. Neither do the files.

## Demo

The 60–90 second demo script is in [DEMO.md](https://github.com/BhushanVidhate-27/eventually/blob/main/DEMO.md). No hosted server: local Ollama plus 20 generated filenames.

```bash
python3 eventually.py --demo
python3 eventually.py demo-downloads --apply
python3 eventually.py --undo
```

Three beats: plan (20 files, 11 folders, 9.7s, zero bytes uploaded), apply (a boring tree), undo (all 20 files back). The last beat matters most: a lazy tool should be easy to reverse.

## Code

Code: [github.com/BhushanVidhate-27/eventually](https://github.com/BhushanVidhate-27/eventually). One Python file, standard library only, plus 21 tests and an MIT license.

No pip install. No API key. No telemetry.

## How I Built It

I started with the chore, not the model. The chore is opening Downloads, seeing `lease-agreement-signed.pdf` next to `Figma.dmg` and `voice-note-mom.m4a`, making twenty tiny decisions, then closing the window. I wanted a tool that would make the decision once and then get out of the way.

The implementation is plain Python: `pathlib` for the folder, `urllib` for Ollama's `/api/chat` endpoint, and a JSON schema to constrain the model to the 11 folder names. Each file gets one request. The model returns a folder, a confidence, and a one-line reason; the plan groups the results and prints them.

The first version sent all 20 filenames in a single prompt. The 1B model looped, repeated names, and invented folders. I switched to one filename per request, four in flight, temperature 0, and a fixed seed. It was slower per request and far more reliable: twenty small decisions, about half a second each. Slow and steady was also the better engineering choice.

A 1B model still makes obvious mistakes. It once put `main.py` in Spreadsheets. A thin guardrail layer catches those: source-code extensions are Code, installers are Installers, a receipt photo is a Receipt, a text note is a Document. The model still decides the ambiguous cases — is this PDF a lease or an invoice? — but it is never trusted with an obvious extension. If the model errors, the file goes to To Sort, not a guessed folder.

Safety is the rest of the product. The default is a dry run. It only looks at top-level files, skips hidden files, symlinks, and in-progress downloads, and never overwrites; a collision becomes `report (2).pdf`. `--apply` writes a manifest to `~/.eventually/last-run.json`, and `--undo` replays it in reverse. The tests use a fake classifier, so they run without a model: parsing, guardrails, discovery, collisions, apply, undo, and model failure. All 21 pass.

## Why Does Open Innovation Matter?

Look at the filenames a tool like this sees: `lease-agreement-signed.pdf`, `invoice-october-acme.pdf`, `tax-2025-final-v2.pdf`, `voice-note-mom.m4a`. That is not just file metadata. It is a sketch of my finances, my home, and my family. Sending it to a closed API to avoid opening Finder is a bad trade.

Local open weights make the private version the default. The brain is a 1.3 GB file on my disk. It works on a plane, needs no key or credit card, and cannot be deprecated by a pricing page. The harness is open source too, so anyone can read every line and confirm that the only network call is to `localhost:11434`. That is a different kind of trust than a privacy policy.

Open innovation also makes small problems legible. I do not need a frontier model to know that a `.dmg` is an installer. A 1B model plus a careful harness is a complete product for a chore that would never justify a startup, a subscription, or a data pipeline. Open weights gave me permission to build something small and local, and open source gives the next person permission to change the folder names and make it theirs.

## The Lazy Payoff

I measured the demo on this machine: an Apple M2 Mac running macOS 27.0.1, with `llama3.2:1b` already loaded. Eventually planned 20 files in **9.7 seconds**. My honest manual baseline is about **4 minutes** for the same folder, because the time is mostly deciding, not dragging. That is roughly **3.8 minutes back every week**, or **3.3 hours a year**, plus one less open loop in Finder.

The number I care about is smaller and harder to measure: I no longer feel a small tax every time I open Downloads. The folder is either tidy or it is waiting for one command. Either way, it is not asking me a question.

Laziness should be reversible. Eventually plans before it moves, never overwrites, and keeps an undo manifest. That is the difference between automating a chore and creating a new one.

One command, one job, finished.

#devchallenge #hf26challenge
