# OttR Open Connection Interface (OCI)

**OCI Version: 0.2 (draft).** This file is what the OttR Open Connection
License means by "Open Connection Interface". Each release of OttR ships its
own copy of this file, and the copy in a release decides the obligations of
Products built on that release (License, Section 3.1).

Items marked TODO need your decision before the first release under the
license. Numbers marked "proposed" are starting points to adjust.

## 1. Connection mechanism

TODO: choose one primary mechanism and describe it here, with its address,
message format and an example call. Options to consider:

- A local HTTP/JSON API bound to `127.0.0.1` by default.
- JSON-RPC over standard input and output (simple for plugins started by a host app).
- A documented Python plugin API (works only for Python hosts).

Whatever you pick, a developer must be able to connect to it from a separate
program without reading OttR's source code.

## 2. Minimum Capabilities and conformance tests

Every Product that contains OttR must keep these available through the OCI and
must pass the test for each one. The tests are written so that they do not
depend on the mechanism chosen in Section 1.

| ID | Capability | Conformance test (pass criteria) |
|----|------------|----------------------------------|
| C1 | List | A reference client asks for the learned apps and saved skills and receives every one of them by name. |
| C2 | Plan | The client sends a plain-language request that matches a saved skill and receives an ordered plan and a plan ID. Nothing is clicked or typed on the desktop. |
| C3 | Run | After the plan ID is approved, the task runs. A plan ID that was never approved is refused and nothing runs. |
| C4 | Confirm and abort | The client can approve or cancel a plan. An abort request stops a running task before its next step starts. |
| C5 | Status and result | After a run, the client can read each step's result (`verified`, `unverified` or `failed`) and the final outcome. |

TODO: decide whether teaching (`/learn`, `/teach`) is a Minimum Capability or
an optional extra.

## 3. What the OCI never exposes

- Passwords. They are asked for at run time and never stored or returned.
- Raw recordings (`raw_recording.json`), which can contain typed text.

## 4. User consent

A Third-Party App must be approved by the End User before it can run tasks.
The approval prompt may be provided by the host, but it must not require
payment or the host's permission (License, Section 3.6).

## 5. Versioning and compatibility

The interface version has the form `MAJOR.MINOR`.

**Backward compatible** means that a client written against a version of the
interface, and using only behavior documented in that version, keeps working
unchanged.

**Breaking change** means any of the following:

- removing or renaming a Minimum Capability, a request field or a response field;
- changing the meaning, type or allowed values of a field, or the meaning of an error code;
- making an optional field required, or adding a required field to a request;
- changing the connection mechanism, address or authentication handshake so that an existing client can no longer connect.

**Not a breaking change:** adding a new capability, adding an optional request
field, or adding a new response field that existing clients can ignore.

Rules:

- A breaking change requires a new MAJOR version. Other changes bump MINOR.
- A Product must support the MAJOR version in its OCI Version and must not
  remove or break anything documented in it.
- A Product may also support newer versions.

## 6. How conformance is shown

A Product conforms if the reference client, run by someone other than the
Product's developer, passes tests C1 to C5 against the OttR Component inside
the Product, without a payment, a registration with the host or the host's
approval (End User consent under Section 4 is allowed).

TODO: add a `conformance/` script to the repository that runs C1 to C5, so
that "compatible" can be tested instead of argued. Until it exists, the tests
in Section 2 are the reference.

## 7. Limits and abuse

**Minimum limits** (proposed). A host may set higher limits but not lower ones:

- C1 and C5 (read-only): at least 60 requests per minute for each Third-Party App.
- C2 and C4: at least 10 requests per minute for each Third-Party App.

**One task at a time.** OttR drives the real mouse and keyboard, so only one
task can run on a machine at once. A host may serialize C3 requests. If it
does, it must queue them or answer with a documented "busy" response, and it
must treat its own software and Third-Party Apps the same way.

**Malicious or abusive** (License, Section 3.2) means a Third-Party App that:

- tries to get around End User consent, or to read data this OCI never exposes;
- runs tasks the End User did not approve;
- keeps exceeding the minimum limits after the host has told it so; or
- attacks or overloads the Product.

## 8. Changelog

- 0.2: conformance tests, definitions of breaking change, limits and abuse.
- 0.1: first draft.
