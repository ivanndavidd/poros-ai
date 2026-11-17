"""Generated report packages (Monthly Business Review first).

A report is a background job that runs a FIXED set of governed SQL templates for one
brand and one month, computes the derived figures in Python, and writes a package of
files (Markdown now; workbook and deck in later phases) stored encrypted per tenant.

Numbers never come from a model here: every figure is a cell of a governed query result
or arithmetic over those cells. Data that does not exist yet does not stop the report —
the section is emitted as a documented placeholder and listed in OPEN-ITEMS.
"""
