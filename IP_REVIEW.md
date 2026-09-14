# Preliminary duplicate-detection IP search

Reviewed: 2026-09-13. Scope: the current local scanner, an English-language public patent search, and relevant open-source implementation evidence. No distribution country was specified; the patent examples below concern US and Japanese records. This is a preliminary technical comparison, not a legal freedom-to-operate opinion.

## Result

Related duplicate-detection patents and published applications exist. The broad size/hash/byte-comparison approach also appears in existing open-source software. This search does **not** establish that someone owns this exact implementation, that an enforceable claim covers it, or that it is patent-free. Patent infringement requires comparison with the applicable claims, rather than a title or abstract. See the [USPTO's explanation of patent infringement](https://www.uspto.gov/patents/basics/manage).

## Implementation examined

The current [scanner](duplicate_cleaner/scanner.py) performs:

1. Grouping by file byte length in `scan`.
2. SHA-256 of up to three 64 KiB samples in `sample_digest`, using sorted unique offsets `0`, `max(0, (size - 65536) // 2)`, and `max(0, size - 65536)`.
3. Full-file SHA-256 in `full_digest` for surviving groups.
4. Direct comparison through EOF in `compare_streams` / `identical`; hash collisions are separated into distinct groups.

Sampling is independent of filename extension. The scanner uses 1 MiB read chunks and checks file identity/metadata around reads. It does not implement a remote fingerprint database or a camera-to-server transfer protocol. [Cleanup](duplicate_cleaner/cleanup.py) compares contents again before recycling selected files.

## Relevant records

Status descriptions are what Google Patents displayed when consulted. They have not been confirmed with the relevant patent offices and should not be relied on as current enforceability determinations.

| Record | Relevant disclosure or claim | Comparison and status limitation |
| --- | --- | --- |
| [US8660998B2 — NEC, Duplicate file detection device, duplicate file detection method, and computer-readable storage medium](https://patents.google.com/patent/US8660998B2/en) | Priority 2011-03-23; granted 2014-02-25. Claim 1 includes extension-specific hash-input definitions and an external examination request concerning file servers, with mediated results. Its background describes earlier size/partial-hash/full-hash detection. | This scanner does not select hash inputs by extension or use that external request workflow. These are technical distinctions, not a complete claim analysis. Google labels the US grant **Expired - Fee Related**; official status and any related rights require verification. |
| [JP2007201861A — Eastman Kodak, File management method](https://patents.google.com/patent/JP2007201861A/en) | Priority 2006-01-27; published 2007-08-09. Describes matching image-file sizes, then beginning-part hashes, then whole-file hashes when managing camera/server transfers. | Strong similarity to the filtering sequence; this app samples additional locations and always verifies every byte. This is an application publication, not proof of a granted enforceable patent. Google's Pending label conflicts with its listed 2012 refusal event; Japanese prosecution status is unresolved. |
| [US7310644B2 — Microsoft, Locating potentially identical objects across multiple computers](https://patents.google.com/patent/US7310644B2/en) | Priority 2001-06-06; granted 2007-12-18. Claim 1 includes generating file information and transferring it to a database server for comparison with information from other computers. | This app performs its comparisons locally without transferring fingerprints to that service. Google labels this US grant **Expired - Lifetime**. Related claims and official records were not exhaustively examined. |

The distinctions above do not establish non-infringement. Adding a final verification step or substituting SHA-256 for another hash does not, by itself, resolve whether a claim covers a process.

## Existing software evidence

The upstream [fdupes manual](https://raw.githubusercontent.com/adrianlopezroche/fdupes/master/fdupes.1) documents size and MD5 comparisons followed by byte comparison. Its [source](https://raw.githubusercontent.com/adrianlopezroche/fdupes/master/fdupes.c) provides further implementation evidence. This supports describing the general approach as established practice, rather than claiming it as a new invention. This review did not establish dated prior art sufficient to invalidate any particular claim. Existing open-source software is not a patent license for this app.

## Licensing and remaining scope

The repository now uses MIT for its original code and documentation. Dependencies retain their own licenses; binary distribution requirements are described in the README. Under US copyright principles, code expression and the underlying method are distinct, and copyright does not require registration to arise. See [USPTO copyright basics](https://www.uspto.gov/ip-policy/copyright-policy/copyright-basics). Adding MIT neither registers IP nor grants rights belonging to third-party patent owners.

Searches included duplicate file detection with size, partial/full hashes, byte comparison, and beginning/middle/end sampling, plus cited publications. Unpublished applications, exhaustive patent families/continuations, official maintenance-fee or revival records, prosecution histories, and country-specific legal analysis were not covered. Vietnam-specific rights were not searched. The app name and trademark availability were outside this method-focused review, as was a full source-provenance audit.

For a release decision requiring legal certainty, a patent professional would need the intended countries and a claim-by-claim review of relevant live rights. Preserve the source version and this report as inputs to that review; do not describe the app as patent-cleared based on this search.
