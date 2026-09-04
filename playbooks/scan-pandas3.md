# Scan a repository for work a dependency bot cannot finish

## Overview

You are looking, not fixing. Read the repository for places where a major library version change
alters behaviour, and report what you find. You will not change a line, open a pull request, or
comment anywhere. Each finding becomes a ticket that another session may later be asked to fix,
so a wrong finding costs someone real time.

## Required from User

- The repository and the branch to read.
- The library, the version in use and the version being moved to.
- The classes of change to look for, and the paths to stay out of.

## Procedure

1. Read the library's own upgrade notes for the two versions named. Work from what they say
   changes, not from memory.
2. Search the repository for call sites that match those changes. Prefer running the project's
   own tests with warnings promoted to errors, because a warning the library itself emits is
   evidence and a guess is not.
3. For each site, read the surrounding function. Decide whether behaviour actually changes here,
   or whether the call is already safe.
4. Discard anything already covered by an open ticket or already fixed on the branch.
5. Record at most the number of findings you were asked for, most important first. Important
   means, in this order: it breaks outright rather than only warning; a command you ran
   demonstrated it rather than you recognising the shape of it; a test already covers the site,
   so the fix can be checked. Only that many are kept, so do not spend them on the easy ones.
6. For each finding give the file, the line, the class of change, and why you believe it: quote
   the library's message where there is one, and name the tests that cover the site.
7. Mark confidence honestly. Use certain only when a command you ran demonstrated the change.

## Forbidden Actions

- Changing any file, opening a pull request, pushing a branch, or commenting on an issue.
- Reporting a site you have not read.
- Reporting anything under the paths you were told to stay out of.
- Filling the list to reach the maximum. Fewer, better findings are the point.

## Acceptance

- Every finding names a real file and line that exists on the branch.
- Every finding says how you know, not that you think so.
- Nothing in the repository changed.

## Result

Provide structured output matching the findings schema and call provide_structured_output with
is_final=true. Fields: searched, and findings with title, file, line, class, why, tests and
confidence. The session is done when that call has been made.
