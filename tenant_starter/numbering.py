"""How this school numbers its people.

This file belongs to THIS school. It sits in the school's own folder (tenants/<code>/numbering.py),
so changing it changes only this school and never touches any other. Brightstars Academics
puts a copy here when the school is created, and reads it again whenever it changes: save the
file and the next number is made by the new rule, with no restart.

    candidate_code(ctx)   the sign-in code of a new entrance-exam candidate     (below)
    student_number(ctx)   how a student's number is written                      (optional, at the end)

Each is an ordinary Python function. It receives ``ctx`` (what it can look at) and returns the
text of the code. Two things are always checked afterwards, so a slip cannot do damage:

  * the code must be free. If it is taken, the function is called again with ``ctx.attempt``
    raised by one, so a counter can simply move on to the next number;
  * the code must be safe to type, print and put in a file name: letters, digits, "." "_" "-"
    (candidate codes are made UPPER-CASE, because that is how candidates sign in), at most 40
    characters.

If this file has an error, registering a candidate is refused with a plain message and nothing
is saved. It never quietly falls back to another rule, so a school's codes always follow ITS rule.

A word of care: this file is program code and runs with the same power as the application.
Only the people who run the platform should edit it. School staff cannot change it from the
portal, and it must never be filled from anything a user typed.

What ``ctx`` offers to candidate_code:

    ctx.school_code            the school's code in capitals, e.g. "ABC"
    ctx.school_name            the school's name
    ctx.year                   this year, e.g. 2026 (the server's local time)
    ctx.now                    the current date and time (a datetime)
    ctx.candidate_name         the candidate's name, if known (else "")
    ctx.target_class           the class applied for, e.g. "JSS 1" (else "")
    ctx.attempt                0 on the first try, +1 each time the code returned was taken
    ctx.last_number(prefix)    the biggest number already used after this prefix (0 if none)
    ctx.next_in_sequence(prefix, digits=4)
                               prefix + the next number, padded with zeros, e.g. "ABC-2026-0007"
    ctx.taken(code)            True if a candidate already has this code
"""


def candidate_code(ctx):
    # ABC-2026-0001, ABC-2026-0002 ... starting again at 0001 each year.
    prefix = f'{ctx.school_code}-{ctx.year}-'
    return ctx.next_in_sequence(prefix, digits=4)


# ---------------------------------------------------------------------------------------------
# Student numbers. Optional: delete the "#" at the start of each line to use your own rule.
#
# Without this function the school's numbering policy decides (a prefix, optionally the year, and
# a running number, e.g. ABC/2026/0001). The running number, and the record that a number was
# issued, are still kept by the platform; this function only chooses how the number is WRITTEN.
#
#   ctx.prefix          the policy's prefix
#   ctx.include_year    True if the policy asks for the year
#   ctx.year            the year the number is issued in
#   ctx.sequence        the next running number, a whole number, never repeated
#   ctx.padding         how many digits the policy asks for
#   ctx.school_code, ctx.school_name, ctx.now      as above
#
# def student_number(ctx):
#     # 26/0001 : two-digit year, a slash, the running number.
#     return f'{ctx.year % 100:02d}/{ctx.sequence:04d}'
