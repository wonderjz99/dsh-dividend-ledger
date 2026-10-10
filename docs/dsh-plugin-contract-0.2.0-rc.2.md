# DSH plugin authoring contract — `0.2.0-rc.2`

Host-plugin / tool / config / subprocess / skill / install / client contract for DeepSeek Harness as shipped in the installed desktop app. No source checkout exists on this machine: the archive's `README.md` files are the authoritative contract text and the bundled `lib/*.js` the authoritative implementation (no `.d.ts` ship in these packages).

## 0. Provenance, legend, method

| Fact | Value | Read from |
|---|---|---|
| CLI runtime version | `0.2.0-rc.2` | `ASAR:/dsh/node_modules/@deepseek-ai/dsh/package.json` |
| Desktop shell | `0.2.0-rc.2` (`@deepseek-ai/dsh-desktop`) | `ASAR:/package.json` |
| Build commit | `5e9e301dd9dc8923b2762f76dacfc5751f6ca851` | `ASAR:/package.json` → `dshBuildCommit` |
| Archive mtime (install anchor) | `2026-09-29 17:55:06` | `stat` on `app.asar` |
| Machine date at verification | `2026-10-10 05:46 UTC` | `date -u` |

`ASAR` = `/Applications/DeepSeek Harness.app/Contents/Resources/app.asar`; members cited as `ASAR:/dsh/node_modules/...`. Tags: **[V]** verified here (archive / profile / `node_modules`, or confirmed by executing the code) · **[D]** documented in a bundled `0.2.0-rc.2` README · **[I]** inferred from ≥2 documented facts (reasoning inline) · **[C]** community claim, unverifiable here · **[C✗]** community claim refuted by the bundle.

## 1. Minimum viable host plugin

**1.1 `package.json` [V]** — pattern from `ASAR:.../dsh-tool-fs/package.json`, `dsh-base`, and local `dsh1024`:

```json
{
  "name": "dsh-my-plugin", "version": "0.1.0", "type": "module", "main": "lib/index.js",
  "exports": { ".": { "types": "./lib/types/index.d.ts", "default": "./lib/index.js" },
               "./cordis.patch.yml": "./cordis.patch.yml", "./package.json": "./package.json" },
  "files": ["lib", "cordis.patch.yml"],
  "peerDependencies": { "@deepseek-ai/cordis": "~4.0.4", "@deepseek-ai/dsh-tools": "0.2.0-rc.2" },
  "peerDependenciesMeta": { "@deepseek-ai/cordis": { "optional": true } },
  "dsh": { "bundle": { "patch": "./cordis.patch.yml" } }
}
```

`dsh.bundle.patch` is **one patch file path or an ordered list** of them, each relative to the package root; a list is applied in order as one bundle layer **[D]** (`ASAR:.../dsh-package-manifest/README.md`). `dsh.client` is browser-only (§8). `dsh.manifestVersion` and `engines.dsh` are optional and **not enforced** by current installers; the enforced mechanism is `peerDependencies` on `@deepseek-ai/dsh` / `@deepseek-ai/dsh-*` (§7.6) **[D]**. ESM (`type: "module"`) is universal across all inspected packages **[V]**.

**1.2 `cordis.patch.yml` entry format [V]** — a YAML top-level array with two entry kinds (official example: `ASAR:.../dsh-base/cordis.patch.yml`):

```yaml
# A) INSERT rows (bundle style)
- insert:
    - id: my-plugin                 # row id — the stable handle every later patch targets
      name: 'dsh-my-plugin'         # module specifier (resolution rules below)
      config: { someOption: 1 }     # optional; passed to apply() as the 2nd arg
      disabled: false               # optional
# B) TARGET an existing row by id (override / disable)
- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"   # optional name assertion
  config: { model: deepseek-flash }
```

- A patch entry **replaces the target row's whole `config`** — nothing deep-merges; restate every field you keep. Later layers override by id, last write wins per row **[V]** (`dsh-base` patch header; `dsh-base` README "Known Limitations").
- `name` may be a package specifier, a **subpath export** (the real base patch uses `name: '@deepseek-ai/dsh-plugin-manager/tools'`), an absolute path, a file URL, or a patch-relative `./`/`../` path — "Patch loading converts absolute paths and patch-relative `./` or `../` paths to file URLs within `insert` rows and their nested groups; existing-entry name assertions and replacement `config` values remain literal" **[D]** (`ASAR:.../dsh-app-boot/README.md`).
- `!!js` expressions evaluate at boot (real: `disabled: !!js "!ctx.get('profileContext')"`, `mode: !!js process.env.DSH_TOOLS_MODE`).
- Layer order: each bundle in `dsh.profile.bundles` → profile `cordis.patch.yml` → home-level patch (which therefore outranks it) → `--patch` overlays **[V]** (`/Users/jim/.dsh/profiles/desktop/cordis.yml` comment + app-boot README). An **empty or comments-only** patch file **fails boot**; disable the layer with `[]` instead **[V]** (`cordis.yml` is `[]`).

**1.3 How `name` / `inject` / `Config` / `apply` are consumed [V].** Cordis imports the row's module and calls `apply(ctx, config)`; `inject` names the services that must exist first; effects are filed into the calling fiber and disposed with it (`ASAR:.../cordis/README.md`: "`inject` tells Cordis which services must exist before the plugin runs… Effects, event listeners, and services are removed when their owning fiber is disposed").

- **`Config`** (a schemastery schema, §3.1) is the plugin's config schema. The loader validates and fills defaults **before** `apply` and passes the resolved object as argument 2. Proof by omission in `ASAR:.../dsh-tool-fs/lib/index.js`: `apply(ctx, config)` does `const resolved = config` then reads `resolved.readLimit`, while `Config` declares `readLimit: z.number().default(READ_LIMIT)` — so defaulting cannot be happening inside `apply`.
- **`name`** is optional, used for loader diagnostics. `dsh-tool-fs` declares `const name = "tool-fs"` but exports only `{ Config, apply, inject }`, while `dsh1024` and `dsh-agent-instructions` do `export const name` **[V]**. Patches address the row **`id`**, not the module's `name`.
- **Optional/late services** use `ctx.inject([...], cb)`, not a hard `inject` entry. Real code **[V]**, `/Users/jim/.dsh/profiles/desktop/node_modules/dsh1024/lib/index.js`:

```js
export const name = 'dsh1024'
export function apply(ctx, config = {}) {
  const resolved = { profile: config.profile ?? argvProfile() ?? 'web', /* … */ }
  ctx.inject(['webServer'], hostContext => {
    hostContext.effect(() => mountMarketRoutes(hostContext.webServer, resolved), 'dsh1024: http routes')
  })
}
```

`ctx.effect(fn, label)` registers a reversible side effect on the fiber.

**1.4 Complete minimal working host plugin.** `package.json` as §1.1, plus this `cordis.patch.yml`:

```yaml
- insert:
    - id: hello
      name: 'dsh-hello'
      config: { greeting: 'Hello' }
```

and this `lib/index.js`:

```js
import z from '@deepseek-ai/schemastery'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'hello'
export const inject = ['tools']
export const Config = z.object({ greeting: z.string().default('Hello') })

export function apply(ctx, config) {
  const unregister = ctx.tools.register(defineTool({
    name: 'hello',
    description: 'Return a greeting. Argument `who` names the recipient.',
    parameters: {
      who:   { type: 'string', required: true, description: 'Recipient name.' },
      style: { type: 'string', enum: ['plain', 'shout'], description: 'Rendering style. Defaults to plain.' },
    },
    output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] },
    async execute(args, exec) {
      if (exec.signal.aborted) throw new Error('aborted')
      const text = `${config.greeting}, ${args.who}!`
      return args.style === 'shout' ? text.toUpperCase() : text
    },
  }))
  // ctx.tools.register() already files the disposer into this fiber; unregister() is available for manual control.
}
```

**1.5 Reference implementations already on this machine [V]:** `/Users/jim/.dsh/profiles/desktop/node_modules/dsh-grill-me/` (pure-skill bundle: `ctx.skills.registerProvider`, frontmatter parsing, `inject = ['skills']`); `.../node_modules/dsh1024/` (host plugin: `ctx.inject(['webServer'])`, `ctx.effect`, `dsh.client`, tsdown-built client); `.../node_modules/dsh-plugin/` (`dsh.client`, tsdown client build, `peerDependenciesMeta.optional`).

## 2. Registering an agent tool

**2.1 Exact API [V]+[D].** `import { defineTool } from '@deepseek-ai/dsh-tools'` then `ctx.tools.register(defineTool({…}))`. `defineTool` **and** `ctx.tools` both come from `@deepseek-ai/dsh-tools` ("it provides `ctx.tools`, the registry every tool plugin registers into"; source-map row `src/schema.ts` = "The `defineTool` DSL") **[D]**. Signature verbatim from `ASAR:.../dsh-tools/lib/index.js` (~L2879):

```js
/**
 * Register globally or in the calling agent scope. Scoped tools shadow
 * globals; duplicates within one layer and the reserved `run_code` name fail.
 * @param definition - tool schema, execution, and optional finalization/presentation callbacks.
 * @returns the exact disposer that unregisters the tool.
 */
register(definition) { … }
```

Registration-time validation (same function): `output` must be an object with a **`render` function** — `tool "${name}" must declare output { schema, render, presentationMeta? }`; `output.schema` must be a supported JSON Schema; `timeoutMs` must be a positive finite number; the name `run_code` is reserved. "Registering a tool is enough to make it visible — the registry feeds its schemas into the system-prompt assembly automatically" **[D]**. A tool plugin declares `inject = ['tools']` **[D]+[V]** — every official tool package ends `export { Config, apply, inject }` with `inject = ['tools', …]`.

**2.2 Real code from a bundled official tool [V]** — `ASAR:.../dsh-tool-fs/lib/index.js` ~L262, output schema body and trailing presenters abridged (elisions marked `…`):

```js
ctx.tools.register(defineTool({
  name: 'read',
  description: 'Read a UTF-8 text file and return line-numbered content.',
  parameters: {
    file_path: { type: 'string', required: true, description: 'Path to read, resolved by the filesystem backend.' },
    offset:    { type: 'number', description: '1-based first line to return. Defaults to 1.' },
    limit:     { type: 'number', description: `Maximum number of lines to return. Defaults to ${caps.limit}.` },
  },
  output: {
    schema: { type: 'object', additionalProperties: false, properties: {
      path: { type: 'string', required: true }, offset: { type: 'integer', required: true },
      lines: { type: 'array', required: true, items: { type: 'object', additionalProperties: false,
               properties: { number: { type: 'integer', required: true }, text: { type: 'string', required: true } } } },
      totalLines: { type: 'integer', required: true } } },
    render: (args, value) => [{ type: 'text', text: formatReadOutput(value.path, { … }) }],
    presentationMeta: (_args, value) => ({ path: value.path, /* … */ }),
  },
  isConcurrencySafe: () => true,
  async execute(args, exec) {
    const input = parseReadArgs(args, caps.limit)
    const { target, info } = await resolveRegularReadTarget(ctx, exec, input.filePath)
    const window = await buildWindow(await ctx.fs.streamText(target, exec.signal), { … }, target.displayPath)
    ctx.emit('fs/observed', target, { kind: 'present', version: info.version }, exec)
    return { path: target.displayPath, offset: input.offset, lines: window.lines, totalLines: window.totalLines }
  },
  presentResult(_args, result) { … },
}))
```

Note the shape: **`parameters` is a flat property map** (no `type`/`properties`/`required` wrapper), **`output` is `{ schema, render }`**, and `execute` returns the bare declared value.

**2.3 `execute` contract [V]** (`defineTool` + the registry pipeline in `dsh-tools/lib/index.js`):

- **Signature** `async execute(args, exec)`; `args` is the validated typed argument object.
- **`exec`** fields seen in official tools: `exec.signal` (caller-owned AbortSignal), `exec.agent` (optional; `exec.agent.session`), `exec.callId`, `exec.parent`, `exec.arguments`, `exec.name`. `exec` is `Object.freeze`d before the `tools/result` broadcast.
- **Return value:** the canonical JSON value matching `output.schema`. It is snapshotted, validated against `output.schema` (`ToolOutputError` on mismatch), deep-frozen, then handed to `output.render(args, value)`.
- **`output.render(args, value)`** returns a `ContentBlock[]` — in practice `[{ type: 'text', text: '…' }]` (image blocks exist; the loop checks `content.some(b => b.type === 'image')`). A `render`/`presentationMeta` throw is a projection error.
- **`output.presentationMeta(args, value)`** (optional) produces persisted card metadata; skipped for sub-dispatches (`exec.parent !== undefined`).
- **Errors:** `throw` — `toolErrorResult` wraps it as `[{ type: 'text', text: \`Error: ${message}\` }]` with `isError: true`. Invalid arguments become `ToolArgsError` (`invalid arguments: <violations>`, code `INVALID_ARGS`) **before** your body runs. Per `dsh-tool-fs`'s own comment, only `HarnessError` instances populate `result.error.info` with a structured `code` — a plain `Error` loses it. **A throwing tool does not end the turn** **[D]**.
- **Cancellation is cooperative:** every body receives `exec.signal` and must observe it (`dsh-tool-fs` threads `exec.signal` into every `ctx.fs` call). Cancellation before dispatch is `ABORTED_BEFORE_DISPATCH`; after it, it replaces only a successful outcome with `ABORTED` **[D]**.
- **`timeoutMs` is declarative only** — "the registry never enforces deadlines; enforcement requires the `@deepseek-ai/dsh-tool-call-timeout-policy` wrapper" **[D]** (enforced timeouts surface as `TOOL_TIMEOUT`).
- **Optional definition fields** recognized by `defineTool` **[V]**: `timeoutMs`, `deferLoading`, `isConcurrencySafe(args)`, `projectContent(exec, result)`, `finalizeContent(exec, result)`, `presentCall(args)`, `presentResult(args, result)`.

**2.4 Required metadata.** **Required:** `name`, `description`, `parameters`, `output.schema`, `output.render`, `execute` **[V]**. There is **no `title` and no `annotations` field** on a DSH tool definition — none appear in `defineTool`, in `register`'s validation, or in any bundled tool; model-facing labelling is `name` + `description` alone. `presentCall`/`presentResult` are Host-local render descriptors **the built-in Web client does not consume** — the Web card is derived client-side from `tool.call.toolview`, raw arguments, result content and persisted metadata **[D]**. Shape example from `dsh-tool-todo` **[V]**: `{ card: 'generic', title: 'Update todo list', kind: 'other', rawInput: args.todos }`.

**2.5 Must the plugin declare `dsh-tools` as a dependency?**

- **Import resolution:** DSH installs a runtime resolution table so profile rows resolve `@deepseek-ai/dsh-*` against the single installation ("installs the runtime resolution through Node's ESM and CommonJS resolvers without creating fallback links"; entries "occupy their package names at `$DSH_HOME/profiles/node_modules`") **[D]**. On this machine `/Users/jim/.dsh/profiles/node_modules/` **does not exist** **[V]** — it is created on demand, so the ancestor-lookup half of this mechanism could not be confirmed empirically.
- **Declaration drives the compatibility gate:** before importing a plugin, DSH checks its `peerDependencies` on `@deepseek-ai/dsh` and `@deepseek-ai/dsh-*` against `getDshRuntimeVersion()`; every declared range must match, and "Missing DSH peers impose no constraint" **[D]**. An omitted peer is tolerated; a *wrong* range gets your row denied/skipped.
- **For an externally linked dev plugin the peer is load-bearing [I]:** the documented rule is "A profile link to an external directory admits its importers to peer-aware ancestor lookup… At each `D/node_modules` position, **current `D/package.json` peer names present in the runtime table use the runtime package**; other names use the physical candidate" **[D]**. A `file:`/`link:` install makes your working tree the external directory, so its bare `@deepseek-ai/dsh-tools` import is served by the runtime table **only if that name is in its `peerDependencies`**. Declare it; keep build tooling (`typescript`, `@types/node`) in `devDependencies`.

**2.6 Community disagreement — resolved in favour of the bundle [C✗].** `https://raw.githubusercontent.com/Wenaixi/dsh-plugin-dev/HEAD/references/tools.md` shows `parameters: { type: 'object', properties: {...}, required: ['text'] }` and `output: { type: 'object', properties: {...} }`. **Invalid for `0.2.0-rc.2`:** `ctx.tools.register` throws `tool "…" must declare output { schema, render, presentationMeta? }`, and `parameterSchemaSpecToJsonSchema` rejects `type`/`properties` as unsupported author keys. `https://raw.githubusercontent.com/omdsh-dev/dsh-plugin-dev/HEAD/skills/dsh-plugin-dev/references/tool-plugin.md` agrees with the bundle on every point (property-map `parameters` with `required: true`, `output: { schema, render }` returning `ContentBlock[]`, `inject: ['tools']`, cooperative `timeoutMs`, errors via `throw`). Both community docs target `^0.1.1-rc.2` peers, but the contract they describe matches the bundled `0.2.0-rc.2` implementation.

## 3. Schemas

Two independent systems; do not mix them.

**3.1 Plugin config → schemastery [V].** `@deepseek-ai/schemastery` (`~3.18.4`; a real dependency of `dsh-tools` and `dsh`), imported in official code as `import z from "@deepseek-ai/schemastery"` (`dsh-tool-fs/lib/index.js` L1). Export it as `Config`:

```js
export const Config = z.object({
  readLimit: z.number().default(READ_LIMIT),
  greeting: z.string().default('Hello'),
  mode: z.union(['red', 'blue']),          // enum
})
```

Per `ASAR:.../schemastery/README.md`: `Schema.object/array/dict/tuple/union/intersect/transform/const/is`; instance methods `.required()`, `.default(v)`, **`.description(text)`**; `.volatile()` for live-editable fields sampled per call; `default` and `required` are mutually exclusive; shorthand (`String`/`Number`/`Boolean`/`Date`/literals); `.simplify()` strips defaulted keys; exposes a Standard Schema `~standard` property.

**3.2 Tool parameters/output → the `defineTool` JSON-Schema-subset DSL [V].** Authoring layer: `ASAR:.../dsh-tools/lib/types/schema.js`. **No schemastery and no raw-JSON-Schema escape hatch** in the `parameters` position. `parameters` is an implicit property map; `compilePropertyMap` emits `{ type: 'object', properties: {…}, required?: [...] }` — `required` only when non-empty, and root openness is not overridden.

| Node `type` | Allowed keys besides annotations |
|---|---|
| `string` / `number` / `integer` / `boolean` / `null` | `type`, `enum`, `const` |
| `array` | `type`, `items` |
| `object` | `type`, `properties`, `additionalProperties` (**must be explicitly `true` or `false`**) |
| `json` | `type` (author-only; compiles to an annotation-only schema = any JSON) |
| `oneOf` | `oneOf` (≥2 value schemas); **cannot be combined with `type`** |

Shared **annotations** on any node: `description`, `title`, `default`, `examples`. **Optional unless `required: true`** — and `required` accepts only `true` (`{ required: false }` throws `… .required must be true when present`). **Enum:** `enum: [...]` on a scalar node. Anything outside this vocabulary throws `JsonSchemaError` at definition time — the DSL is enforced, not advisory.

```js
parameters: {
  command:  { type: 'string', required: true, description: 'Shell command.' },
  timeoutMs:{ type: 'number', description: 'Override the default. Capped by configuration.' },
  mode:     { type: 'string', enum: ['fast', 'safe'] },
  meta:     { type: 'object', additionalProperties: false, properties: { tag: { type: 'string' } } },
  tags:     { type: 'array', items: { type: 'string' } },
},
output: { schema: { type: 'string' }, render: (_a, v) => [{ type: 'text', text: String(v) }] },
```

Community output-shape guidance (all three patterns confirmed in the wild) **[C]**: `{ type: 'string' }` holding JSON text, `{ type: 'json' }` for a structured value, or a scalar. The bundled `read` tool uses a fully typed object schema **[V]**.

## 4. Config injection and runtime locations

**4.1 Own config.** `apply(ctx, config)` — `config` is already schema-validated and defaulted by the loader (§1.3); no `ctx.config` read is needed. The profile patch **replaces the row's whole config**, so `cordis.patch.yml` must restate every field it keeps **[V]/[D]**.

**4.2 `$DSH_HOME` [D].** `@deepseek-ai/dsh-home-paths`:

```js
import { resolveDshHome, dshHomePath, dshCachePath, expandHomePath } from '@deepseek-ai/dsh-home-paths'
const home = resolveDshHome()          // explicit override → $DSH_HOME → ~/.dsh
const settings = dshHomePath('settings')
```

An explicit configured path beats `$DSH_HOME`, which beats `~/.dsh`; a blank/whitespace `$DSH_HOME` counts as unset. Profiles live at `$DSH_HOME/profiles/<name>` **[D]**; here `/Users/jim/.dsh/profiles/desktop/` **[V]**.

**4.3 Session workspace / cwd — there is no process-wide "current workspace".** It is a per-session immutable fact. `ASAR:.../dsh-tool-bash/lib/index.js` (~L294) **[V]**:

```js
function resolveWorkdir(modelWorkdir, exec, policyWorkspaceRoot) {
  const headerCwd = exec.agent?.session.header.cwd
  const sessionCwd = policyWorkspaceRoot ?? headerCwd
  if (modelWorkdir === undefined) return sessionCwd
  if (sessionCwd !== undefined && !isAbsolute(modelWorkdir)) return `${sessionCwd}${sep}${modelWorkdir}`
  return modelWorkdir
}
```

So inside a tool: **`exec.agent?.session.header.cwd`**. Outside a tool execution there is no session — which is why `dsh-sandbox-policy` **[D]** exposes `resolve({ session, mode })` → one per-call policy `{ mode, workspaceRoot, … }`, precedence approved explicit mode > session's last `sandbox/mode` event > deployment default. `workspaceRoot` config default is `process.cwd()`, used only "for agentless calls or sessions without a cwd"; "Normal agent calls use the session's immutable cwd", and "Workspace identity needs no event: the immutable `SessionHeader.cwd` recorded at creation is the root for every call in that session." Only one primary workspace root per session exists.

**4.4 `DSH_*` environment [D].** `@deepseek-ai/dsh-shell-env` gives **every model shell call** (not your plugin process): `DSH_HOME`, `DSH_SHELL=1`, `DSH_SESSION_ID` (agent calls) and, when the launcher supplied a profile context, `DSH_PROFILE` + `DSH_PROFILE_DIR`. Plugins add facts with declared keys:

```js
export const inject = ['shellEnv']
export function apply(ctx) {
  ctx.shellEnv.register({
    name: 'deployment-region',
    variables: { DSH_DEPLOYMENT_REGION: { description: 'Current deployment region.' } },
    resolve: execution => execution.agent === undefined ? {} : { DSH_DEPLOYMENT_REGION: 'cn-north' },
  })
}
```

Every returned key must be declared or the call fails; registration disposes with the plugin. Do **not** read `DSH_*` from your own `process.env` — the subprocess service scrubs inherited `DSH_*` from children.

## 5. Running a subprocess (e.g. a Python interpreter)

**5.1 Why not `child_process.spawn`.** It is technically possible (plugin code is in-process Node) but wrong for three documented reasons: (1) **installed host code runs outside the sandbox** — "installed Host code executes in-process **outside the workspace sandbox**" **[V]** (`ASAR:.../dsh-plugin-manager/README.md`); (2) **confinement lives in the executor, not the process service** — `dsh-bash-sandbox` "awaits confinement of each command's exact `['bash','-c',command]` argv through `ctx.sandbox.confine()`, spawning the returned argv directly" **[D]**, while `ctx.subprocess` documents only executable resolution, argv/stdio/env, bounded collection and managed-range termination — its sole "approval" mention is that the control pipe "carries no authority to bypass tool approval", and its sole "policy" mention is environment scrubbing, so it has **no sandbox and no approval** **[V]** (`ASAR:.../dsh-subprocess/README.md`) and a plugin calling `ctx.subprocess` or `child_process` directly gets an **unconfined** child; (3) **approval is a tool-layer concept** — "Deny-only at the seam — this executor never grants permission; the approval flow lives in the tool layer" **[D]**.

**5.2 Seams to use instead.**

| Service | Package | For |
|---|---|---|
| `ctx.shell` | seam `@deepseek-ai/dsh-shell`; providers `dsh-bash-local` / `dsh-bash-sandbox` | commands with resolved workdir + timeout + output caps, honouring policy |
| `ctx.subprocess` | `@deepseek-ai/dsh-subprocess`; provider `dsh-subprocess-local` | direct-argv spawn / PTY, bounded collection, spill, scrubbed env |
| `ctx.jobs` (+ `dsh-tool-jobs`) | `@deepseek-ai/dsh-jobs` | background work: ids, listing, cancellation |
| `ctx.approval` | `@deepseek-ai/dsh-user-approval` | asking the user before a policy-widening/risky action |
| `ctx.sandboxPolicy` | `@deepseek-ai/dsh-sandbox-policy` | resolving the per-call `{ mode, workspaceRoot }` |

`ctx.shell` contract **[D]** (`ASAR:.../dsh-shell/README.md`) — one execution method, request/spec split:

```js
const execution = await ctx.shell.execute(ctx.shell.resolve({ command: 'ls -la' }))
const result = await execution.result()
if (result.timedOut) console.log('timed out after', result.timeoutMs)
```

`resolve()` is the single place defaults/caps are applied (never `execute`). Expiry policy is `'kill'` (default) or `'none'` (no deadline; the caller bounds its own wait, which is how a foreground command outlives its timeout). Background reads use `readOutput()` (consuming, never repeating; lossy reads point at spill files), `kill()`, and `await handle.done` (never rejects for the direct command). `result()` rejects **only** for infrastructure failures.

**5.3 Bounded timeout + captured stdout/stderr.** `ASAR:.../dsh-bash-local/README.md` **[D]**: `cwd` default `process.cwd()`; `timeoutMs` default `120000` (foreground); `maxTimeoutMs` default `600000` (cap for per-call overrides); `maxOutputBytes` default `64000` (per-stream in-memory cap, overflow spills to a temp file); `maxSpillBytes` default `67108864` (per-stream full-output spill cap); `graceMs` default `3000` (kill-escalation + post-exit pipe-drain grace). Result fields **[V]** (`dsh-tool-bash` `canonicalBashResult`): `exitCode`, `signal`, `timedOut`, `aborted`, `timeoutMs`, `stdout { text, truncated, spillPath? }`, `stderr { … }`, `sandbox { mode, denied, enforcement?, runnerFailed? }`. Non-zero exits are **results, not errors**; only spawn failures/aborts are tool errors.

**Python pattern [I]** — route through the shell seam so sandbox, approval and output budget all apply. Note the real `bash` tool stamps the *resolved policy* onto the request (`ASAR:.../dsh-tool-bash/lib/index.js` L653: the request is `{ command, workdir?, timeoutMs?, dshEnv, sandboxPolicy? }`), which is how a confining executor learns the mode — so pass `sandboxPolicy`, not just `workdir`:

```js
export const inject = ['shell', 'sandboxPolicy']

async function runPython(ctx, exec, scriptPath, args) {
  const policy = ctx.sandboxPolicy.resolve(exec.agent === undefined ? {} : { session: exec.agent.session })
  const spec = ctx.shell.resolve({
    command: `python3 ${JSON.stringify(scriptPath)} ${args.map(a => JSON.stringify(a)).join(' ')}`,
    workdir: policy.workspaceRoot ?? exec.agent?.session.header.cwd,
    timeoutMs: 60_000,          // capped by maxTimeoutMs
    sandboxPolicy: policy,      // required for confinement to apply
  })
  const result = await (await ctx.shell.execute(spec)).result()
  return { exitCode: result.exitCode, stdout: result.stdout.text, stderr: result.stderr.text, timedOut: result.timedOut }
}
```

Caveats **[D]**: a relative `workdir` resolves against the session cwd, and an explicit `workdir` is overridden by a resolved sandbox-policy root so confinement and launch share one identity. `run_in_background` requires both `dsh-jobs-local` and `dsh-tool-jobs`. `sandbox_permissions` without a sandboxing executor fails with "`sandbox_permissions` is not available in this composition (no sandboxing executor to escalate)". If you use `ctx.subprocess` directly instead, `resolveExecutable` rejects relative paths containing separators — give an absolute path or a bare `PATH` name **[D]**.

## 6. Skills

**6.1 `ctx.skills.registerProvider` [V].** From `ASAR:.../dsh-skill/lib/index.js` (~L148): "registration is synchronous, and the returned disposer removes the provider. `runtime` is a reserved provider name." `create` is a **factory** returning the provider; the returned capability also carries `invalidate()`. Working local example — `/Users/jim/.dsh/profiles/desktop/node_modules/dsh-grill-me/index.js`:

```js
export const name = 'dsh-grill-me'
export const inject = ['skills']

export function apply(ctx) {
  ctx.skills.registerProvider(() => ({
    name: PROVIDER_NAME,                       // "runtime" is reserved
    async list(options) {                      // → candidate[]  (summaries, no bodies)
      options?.signal?.throwIfAborted()
      return listed.map(toCandidate)
    },
    async get(candidate, options) {            // → definition | undefined  (full body)
      options?.signal?.throwIfAborted()
      const skill = listed.find(e => e.name === candidate.name)
      return skill === undefined ? undefined : toDefinition(skill)
    },
  }))
}
```

- Both methods receive `{ signal }` and must honour cancellation; `list` must not read skill bodies **[V]/[D]**.
- **Candidate fields** (`dsh-grill-me` `toCandidate`) **[V]**: `name`, `description`, `metadata?`, `invocation`, `provider`, `source`, `resourceBase`, `rank`, `locator`, `path`.
- **Definition fields** (`toDefinition`) **[V]**: the same minus `rank`/`locator`, plus **`content`** (the body). The definition's name must match the selected candidate's or the registry treats the selection as stale, invalidates caches and rediscovers **[D]**.
- Validation is strict: names, descriptions, invocation booleans, provider ownership and a **finite numeric `rank`** are all checked; a malformed candidate fails fast, an invalid skill name returns no skill rather than throwing **[D]/[V]**.
- `ctx.skills.register(skill)` adds **in-memory** embedded skills (registry fills a default invocation policy and the `runtime` provider label; same-name runtime registrations in one layer are first-wins with a warning). There is no TTL — invalidation is provider-driven **[D]**.

**6.2 Frontmatter fields [V]** (`dsh-grill-me` parser):

```markdown
---
name: grill-me                              # required; ^[a-z0-9]+(?:-[a-z0-9]+)*$
description: "…"                            # required, non-empty
metadata: { … }                             # optional; must be a plain object
disable-model-invocation: true              # optional → modelInvocable = false
user-invocable: false                       # optional → userInvocable = false
---
<body>
```

`invocation` = `{ modelInvocable: data['disable-model-invocation'] !== true, userInvocable: data['user-invocable'] !== false }`. Both flags are kept independently, so one discovery serves the model catalog and the user command surface without conflating them **[D]**. Frontmatter must start at byte 0 with a `---` line and end at the next `---` line; the body is trimmed.

**6.3 Rank conventions [V]** — ascending priority, **smaller wins**; resolved as nearest layer wins outright, then within one layer `rank` → provider registration order → provider-local order. Named constants in shipped code: `PROJECT_DSH_RANK` 100 and `PROJECT_AGENTS_RANK` 200, `RUNTIME_RANK` 250 (`ASAR:.../dsh-skill/lib/index.js`), `CUSTOM_RANK` 300, `USER_DSH_RANK` 400, `USER_AGENTS_RANK` 500 (all in `ASAR:.../dsh-skill-filesystem/lib/index.js`), and `BUNDLED_SKILL_RANK` 600 (exported by `dsh-skill`; used by `dsh-grill-me`). A third-party bundled provider should use **600** for parity with official bundled skills; go lower only to deliberately outrank project/user skill files.

## 7. Installing a local, in-development plugin (no npm publish)

**7.1 The `desktop` profile is CLI-locked [V] — the governing constraint here.** `ASAR:.../dsh/lib/bin.js` L36 and `ASAR:.../dsh/lib/plugin-BGnVfe_D.js`:

```js
function rejectElectronProfile(program, profile) {
  if (profile.toLowerCase() === 'desktop')
    program.error('error: profile "desktop" is managed exclusively by the Electron application')
}
// parseDshArgs(argv, version, manageDesktopProfile = false)
//   boot path:   rejectElectronProfile(program, profile)                      ← unconditional
//   plugin path: if (!manageDesktopProfile) rejectElectronProfile(plugin, options.profile)
// versionCommand() → requireDesktopProfile(dir): "Open DeepSeek Harness Desktop once to initialize its profile,
//                    then fully quit it before running dsh plugin --profile desktop."
```

**Consequence:** `dsh plugin --profile desktop add <path>` **cannot be used**. The Electron app passes `manageDesktopProfile = true` internally, so installs into `desktop` go through the app (sidebar **Plugins** page) or the in-app `plugin_manager` tool. Any *other* profile name works normally from the CLI.

**7.2 Method A — install a local path via the plugin manager (best supported) [D]/[V].** `installBundle`'s doc: "`@param spec` One package spec, **including local paths relative to the invocation directory**." The same service backs the Web Plugins page and the `plugin_manager` tool **[D]**. The CLI is a thin pnpm forwarder **[V]** — `dsh plugin --profile <name> <pnpm-args...>` where "pnpm arguments [are] forwarded verbatim (add <pkg>, remove <pkg>, why <pkg>, ...)". So for a non-desktop profile: `dsh plugin --profile my-dev add /abs/path/to/my-plugin`. **Bundle reconciliation is automatic only if you declare `dsh.bundle.patch`** — verbatim from `ASAR:.../dsh-plugin-manager/lib/types/operations.js` L59 **[V]**:

```js
options.onOutput?.(`dsh: warning: ${name} declares no dsh.bundle — installed as a plain dependency, not a profile layer\n`, 'stderr')
```

Declared → the package is pushed into `dsh.profile.bundles` automatically. Not declared → it lands in `dependencies` only and **never loads**. Verify by reading the profile manifest back and comparing `dependencies` against `dsh.profile.bundles`.

**7.3 Method B — insert by absolute path in `cordis.patch.yml` (no install at all) [D].** Since inserted `name`s may be absolute filesystem paths or file URLs (§1.2), point the profile straight at your working tree:

```yaml
# ~/.dsh/profiles/desktop/cordis.patch.yml
- insert:
    - id: my-plugin-dev
      name: '/Users/jim/Projects/dsh-stock-dividend/my-plugin/lib/index.js'
      config: { greeting: 'Dev' }
```

No publish, no pnpm, no `dependencies` entry. **Caveat [I]:** resolution of your plugin's bare `@deepseek-ai/dsh-*` imports from an arbitrary external directory is governed by the runtime-resolution / linked-directory rules in §2.5 — which is exactly why declaring those peers matters. Lowest-risk variant: combine Method A (which creates a real profile link and therefore the documented peer-aware ancestor lookup) with a `link:`/`file:` spec so working-tree edits are picked up.

**7.4 Method C — `--patch` overlay [V].** `dsh --profile <name> --patch <path>` — "extra patch-list overlay applied after the profile layer (repeatable)" (`ASAR:.../dsh/lib/bin.js`). This is the CLI way to try an insert/override without editing the profile. It is a **CLI** flag; I found no evidence the Electron app forwards it, so treat "`--patch` inside the desktop app" as unavailable **[I]**.

**7.5 Pickup: restart vs hot reload.**

- **HMR watches exactly three things: the profile manifest (`package.json`) and both user patch files (profile + home).** "The enabled `dsh-hmr` plugin watches the profile manifest and both user patch files, re-reads the ordered bundle layers, and applies the reload failure policy… **The launcher does not install HMR or watchers; disabled or absent HMR means changes require restart**" **[V]** (`dsh-app-boot` README).
- HMR is **on by default in base-backed profiles**: the base patch ships `id: hmr`, `name: '@deepseek-ai/dsh-hmr'`, `disabled: !!js "!ctx.get('profileContext')"`, `config: { root: [] }` **[V]** (`ASAR:.../dsh-base/cordis.patch.yml`). `root: []` means **module source watching is off** — only configuration is watched. Opt in with `- id: hmr` + `config: { root: ["."] }` **[D]**.
- Therefore a new `lib/index.js` copied over an installed plugin is **not** reloaded unless you enabled module watching; a `cordis.patch.yml`/manifest edit **is** applied live (the manager notes "Direct Plugin Manager operations apply without waiting for file events" **[D]**).
- **Replacing an installed package version still needs a restart:** "Package replacements require restarting the process to load a fresh JavaScript module generation", and "Relinking a profile package needs a restart — Node caches real paths" **[D]**. For the Electron desktop profile, "restart" = fully quit and reopen the app **[I]**.
- Whether the shipped **desktop** profile actually sets `profileContext` (hence whether patch edits hot-apply there) I could **not** verify without booting the app; the web template uses live reload while the other shipped templates apply patches only at startup **[D]**. Treat app restart as the safe default for `desktop` **[I]**.

**7.6 Install-time traps for a local dev plugin.**

- **Peer compatibility gate.** Pre-check for registry specs (`pnpm view … peerDependencies`); post-check for git/tarball specs, which **restores `package.json` + `pnpm-lock.yaml` and reinstalls**. `dsh: installation rejected: Plugin X@V is incompatible with the target DSH release: peerDependencies {…}` plus a `restored …` line means the gate fired, not a network problem **[D]**. A denied bundle is skipped and listed in `skippedBundles` **[D]**.
- **Exemptions are exact-version pairs** in the profile's `compatibility.json` (absent by default): `dsh plugin --profile <name> allow-version <pkg>@<exact> --dsh-version <exact> --accept-risk` **[V]** (parsed in `ASAR:.../dsh/lib/plugin-BGnVfe_D.js`; prints a risk warning to stderr).
- **pnpm 11 ignored build scripts:** a failed install reports `pendingBuilds`; approve via the Web page ("Allow these scripts and retry") or `approvedBuilds` on `install_bundle` **[D]**. Community reports a profile-level `allowBuilds:` entry in `pnpm-workspace.yaml` as the fix **[C]**.
- The desktop profile's `pnpm-workspace.yaml` here is `packages: ['.'] / nodeLinker: hoisted / autoInstallPeers: false` **[V]** — `hoisted` matters: `file:`/`link:` deps still symlink, so the real path is your working tree. Per-install pnpm logs: `/Users/jim/.dsh/profiles/desktop/.plugin-manager/logs/operation-*/pnpm.log` **[V]**.

## 8. Client-side (web UI) extensions — brief

Declaration **[V]** (from `dsh1024` and `dsh-plugin` `package.json`):

```json
"dsh": { "bundle": { "patch": "./cordis.patch.yml" },
         "client": { "platform": "web", "inject": ["@deepseek-ai/dsh-client-locale", "@deepseek-ai/dsh-client-ui-slots"] } },
"exports": { "./client": "./client/client.js", "…": "…" }
```

Contract **[D]** (`ASAR:.../dsh-client-modules/README.md`): a browser plugin declares `dsh.client` with `platform: 'web'`, "exports a `./client` bundle, and lists any non-baseline module requests under `dsh.client.external`". The host scans enabled Loader entries, composes a boot graph injected as `window.__DSH_BOOT__`, and serves bundles under a `/plugins` route (real client rows in `ASAR:.../dsh-web-app/cordis.patch.yml` are marked "dsh.client rows").

- **The bundle must be prebuilt:** "The host serves built client bundles, so `pnpm run build` must have produced each `lib/client.js` before launch; **a missing bundle fails activation loudly** with one build instruction and a package/path list." The README says `lib/client.js` while both real local plugins serve `client/client.js` via `exports["./client"]` **[V]** — the export target is what is served; the README path is the DSH-repo-internal convention.
- **Output format is lazy CJS factories, not plain ESM:** "running a bundle only registers a factory", materialized via `factory(require)`; `require.async` handles code-split chunks; require cycles throw. **Build tool: `tsdown`** **[V]** (local `dsh-plugin` has `"build:client": "tsdown && node scripts/tools/normalize-client-banner.mjs"` and `tsdown 0.22.14` as a devDependency; the client-modules README independently references "A source `import()` split by tsdown"). React and Cordis are in the frozen `PLATFORM_MODULES` seed table; anything else must be declared in `dsh.client.external` and answered by a dynamic package row or an exact static-table key.
- **Not possible without rebuilding the web shell:** you cannot change the shell, add a new *platform seed* module, or inject into a slot the shell does not expose. Removing/replacing the modules bootstrap requires a **page reload**. Client-side HMR is idle unless a rebuild watcher (`pnpm run dev:web`) actually rewrites client bundles **[V]** (`dsh-web-app` patch comment on the `client-hmr` row).

## 9. Verified vs inferred — index

**Verified [V]:** version strings and `dshBuildCommit`; the archive member list and every `lib/*.js` / `cordis.patch.yml` / README quoted above; `defineTool`'s implementation, its DSL validation, and `register()`'s validation + disposer return; `Error: <message>` normalization; the schemastery import in `dsh-tool-fs`; skill ranks 100–600 and `registerProvider`'s factory/disposer shape; `exec.agent.session.header.cwd`; the `desktop`-profile CLI rejection and its guard; the "declares no dsh.bundle — installed as a plain dependency" warning; the plugin-manager "outside the workspace sandbox" statement; the `{ command, workdir?, timeoutMs?, dshEnv, sandboxPolicy? }` shell request; this profile's manifest, patch, `cordis.yml`, `pnpm-workspace.yaml` and `.plugin-manager/logs`; `dsh-grill-me` and `dsh1024` sources.

**Documented by bundled 0.2.0-rc.2 READMEs [D]:** `dsh.bundle.patch` / `dsh.client` semantics; patch layer ordering, `!!js`, empty-patch boot failure; the peer-compatibility gate; runtime resolution and linked-directory lookup order; the tool pipeline and cooperative cancellation; `timeoutMs` being declarative; `ctx.shell` request/spec split and result semantics; `dsh-bash-local` budgets; subprocess executable resolution and env scrubbing; `ctx.skills` merging/validation/staleness; HMR's watch set and restart requirements; the client-modules build requirement and lazy-CJS format.

**Inferred [I]:** declaring `@deepseek-ai/dsh-*` peers is load-bearing for externally linked dev plugins (two documented rules combined); routing Python through `ctx.shell` is lowest-risk (follows from executor-owns-confinement); the Electron desktop profile should be treated as restart-to-pick-up; `--patch` is unavailable inside the desktop app.

**Not verified / uncertain:** `/Users/jim/.dsh/profiles/node_modules/` does not exist here, so the runtime-resolution ancestor lookup was not observed empirically. Whether the shipped desktop profile sets `profileContext` (hence whether patch edits hot-apply in the app) was not tested. The served client-bundle path (`lib/client.js` vs `exports["./client"]`) is stated inconsistently across the bundled README and the real local plugins.

**Community sources (untrusted data; used only where they agree with or are refuted by the bundle):** `https://github.com/Wenaixi/dsh-plugin-dev` (`SKILL.md`, `references/tools.md`, `references/install-resolution-traps.md`) and `https://github.com/omdsh-dev/dsh-plugin-dev` (`skills/dsh-plugin-dev/references/tool-plugin.md`). Wenaixi `references/plugin-anatomy.md` and `omdsh …/references/overview.md` **timed out** on fetch and were not used; `moneka123/deepseek-harness-plugin-dev-guide` and `vlln/plugin-registry` were **not fetched** (context budget). Nothing here depends on them.

**Refuted [C✗]:** Wenaixi `references/tools.md`'s `defineTool` example (`parameters` wrapped in `type/properties/required`, `output` without `render`) is invalid for `0.2.0-rc.2` — see §2.6. Its 16-stage pipeline numbering is self-declared as pedagogical; the bundle documents the real pipeline as `tools/pre-execute` → monotonic guards → `tools/execute` → `tools/post-execute` → `finalizeContent` → `tools/result`. Its rank table (100/200/250/300/400/500/600) **is** corroborated by the bundled sources (§6.3).

## 10. Re-verifying any claim

Extract members from the archive with a ~20-line asar reader: read `uint32LE` at file offset 12 = header size, parse the UTF-8 JSON header at offset 16, then read file bytes at `16 + headerSize - 8 + entry.offset`. Usage: `node asar.mjs list '<regex>' | cat <member> | extract '<regex>' <dest>`. Start with `node asar.mjs list '^/dsh/node_modules/@deepseek-ai/[^/]+/README\.md$'` (~200 official package READMEs, 290 manifests total), then `cat` the specific member. The bundled READMEs are the primary tiebreaker for every contract question above; the bundled `lib/*.js` outranks them for implementation detail.
