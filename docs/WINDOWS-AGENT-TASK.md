# Task for the AI agent on the Windows machine

You are on the Windows machine where this app can actually reach the
enterprise SharePoint MCP gateway. The developer working on this codebase
cannot reach that gateway, and **cannot receive text from this machine** —
the only channel out is a photograph of the screen.

So your job is to **find things out and report them briefly**. Not to
redesign anything.

---

## Constraints — please read before starting

1. **Do not change application code.** If you believe a code change is
   needed, say so in your report with the reason. Someone else will make
   it, in a place where it can be committed and pushed.
2. **Never print secrets.** No API keys, no access tokens, no
   `Authorization` header values, no temporary SharePoint download URLs.
   Report only whether a thing is set, never what it is.
3. **Keep the report short.** It has to be readable in a photograph.
   Aim for under 40 lines. Prefer exact names and key lists over prose.
4. **Read-only.** Do not create, rename, move or delete anything in
   SharePoint. Listing and downloading are fine.
5. If a step fails, **report the failure and stop** rather than trying
   variations. The failure is the useful information.

---

## Step 1 — make sure the environment is current

```
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
backend\.venv\Scripts\python.exe backend\scripts\doctor.py
```

`doctor.py` ends in a verdict. **If the verdict lists problems, stop and
report just that.** Everything below assumes `mcp` is 2.0 or newer.

## Step 2 — walk the four steps and report the shapes

```
backend\.venv\Scripts\python.exe backend\scripts\sharepoint_walk.py
```

This walks exactly what the pipeline walks — look up the site, list the
document libraries, list the folder's files, fetch one file — and prints
the tool it chose, the arguments that tool accepts, and the **key names**
of what came back. It prints no values.

**This output is the single most useful thing you can send.** If it runs
to completion, photograph it and you are done; the rest of this file is
only for when it stops early.

---

## If it stops early: what the developer needs to know

The app has to work against gateways that name things differently, so it
discovers names rather than hard-coding them. Every remaining unknown is
of the form *"what does THIS gateway call the thing I need?"*.

You have the gateway's documentation. Please answer whichever of these
the walk did not:

### A. The exact tool names for these four jobs

| Job | What the app currently looks for |
|---|---|
| Look up a site from its URL or path | `get` + `sharepoint` + `site` |
| List a site's document libraries | `list` + `document` + `librar` |
| List the files in a folder | `list` + `library` + `item` |
| Download one document | `download` + `document` |

Matching ignores the shared `sharepointmcp-` prefix. Report the full
names, e.g. `sharepointmcp-list_library_items`.

### B. The required arguments for each of those four

Names only, and which are mandatory. In particular:

- Does the **site lookup** take a full URL, a hostname plus a path, or a
  site id? What is the parameter called?
- Does **list items** take a folder path, or does it need the folder's
  own item id (meaning you must walk down folder by folder)? What is the
  parameter called?
- Does **download** take `item_id`, `id`, or something else?

### C. Where the answers live in the responses

The app reads these out of the replies. Report the **key names**:

- Site lookup → which key holds the site id?
- List libraries → are the entries under `value`, `items`, something
  else? Which key holds a library's id? Which holds its name?
- List items → same three questions. Also: how is a **folder**
  distinguished from a **file** in that list?
- Download → does the content come back **inline** (base64, under which
  key?) or as a **link** (under which key?)

### D. Two specific things known to differ

1. The browser shows the library as **"Shared Documents"**; the API
   usually calls it **"Documents"**. Which does this gateway expect, and
   which does it return?
2. Files are addressed by an **opaque drive-item id**, not by filename.
   Confirm that, and confirm the id appears in the *listing* response
   (otherwise there is no way to get from a name to an id).

---

## What to send back

Please reply in roughly this shape, and keep it to one screen:

```
DOCTOR      : ok  (or: the problems it listed)
WALK        : reached step N of 4

TOOLS
  site      : <full tool name>   args: <names>  required: <names>
  libraries : <full tool name>   args: <names>  required: <names>
  items     : <full tool name>   args: <names>  required: <names>
  download  : <full tool name>   args: <names>  required: <names>

RESPONSES
  site      : id key = <key>
  libraries : entries under <key>; id = <key>; name = <key>
  items     : entries under <key>; id = <key>; name = <key>;
              folder vs file = <how>
  download  : inline under <key>  OR  link under <key>

LIBRARY NAME: expects <...>, returns <...>
FAILED AT   : <step and the exact error, if any>
NOTES       : <anything the docs say that contradicts the above>
```

If the documentation shows an example call for any of these four tools,
include the argument names from it — that is worth more than anything
else here.
