<!--
The privacy rules every record follows, added to the summarizer's own rules.
Records are raw model output, so anything that must never be written down
belongs here. To give one repository its own rules, copy this file to
.claude/session-memory-rules.md there and edit the copy: it's used in place
of this one. Comments like this one are left out of the prompt.
-->
- Never write a person's name, other than the user's, or their contact
  details: describe a person by their role.
- Never write a customer's or client's name, or data taken from their
  systems, accounts or tickets: say "a customer".
- Never write a hostname, domain name, URL or IP address from a private
  network, or anything read from someone's private traffic.
