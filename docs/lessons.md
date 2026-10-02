# Lessons learned

What went wrong, or nearly did, during Texas A&M's cleanup in 2026. Each one is cheap to avoid
if you know about it in advance.

**Capture supervisors before you need them.** The identity provider forgets a person's supervisor
when they leave. Texas A&M started recording supervisors in April 2026. By July, 38 of 3,133
departed content owners had one on record. Every snapshot should record them, into a history that
a blank never overwrites.

**Log every run, including the ones that "can't go wrong".** Account removals between April and
July changed membership from 13,141 to 11,921, but no record says which run did it or when.
A removal with no results file cannot be audited, explained or undone.

**Back up before deleting content, and verify the backup.** An approved purchase is not a
backup, and neither is a backup that has been started. In the rush before a deadline, some
"never viewed" and "assignment" items were deleted before the backup existed. Write the backup
check into the delete tool so it cannot be skipped.

**Read the condition, not the comment.** A selection query's comment said "users who own items
or groups", but the code required both. It matched 1,021 users where 2,631 were intended, and the
error went unnoticed because the result looked plausible. Check a query against a hand count before
acting on it.

**The text `NULL` is not a value.** Exported spreadsheets wrote empty fields as the literal word
`NULL`. A count that treated it as a value reported 950 supervisors that did not exist. Normalize
empties when you load data.

**Don't download the whole identity provider.** The first identity lookup pulled every account in
the tenant (about 150,000) on each run, taking two to three hours, to match about 12,000 members.
Look up only the accounts you have, in batches.

**Removing an empty single sign-on account is safe.** The account is recreated at the owner's next
login, with nothing lost, because there was nothing in it. That makes empty accounts the right
place to start when the account limit is the problem.

**Moving content to a holding account breaks it.** Items in maps and groups lose those links when
they change owner. Setting an item private keeps the links and still hides it.

**Two numbers for the same thing is normal. Pick one before publishing.** The organization's size
was quoted as 1.5, 1.7 and 1.8 TB in three documents, and the per-user quota as 20 MB and 57 MB.
None was wrong at the time it was written. Say which number you are using and when it was measured.

**Keep personal data out of the code repository from the first commit.** This repository is public
and its history has never held a member list. A pre-commit check (`tools/leak_check.py`) refuses
data files, email addresses, usernames and item IDs. It refused one of our own commits on its
first day, over an example address in a code comment.
