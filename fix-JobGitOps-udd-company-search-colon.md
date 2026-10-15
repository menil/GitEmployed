# AI Decision Record: Preserve Colons in Company Search Sanitization

## Context & Goal
When scraping job postings, `CompanyDeduplicationStore` queries existing GitHub issues by company name using GitHub Search (`in:title "[{clean_comp}]"`). Previously, `sanitize_company_for_search` stripped colons (`:`) along with quotes and brackets.

This caused company names with internal colons, such as `Re:Build Manufacturing`, to be sanitized to `ReBuild Manufacturing`. Because the issues on GitHub are titled `[Re:Build Manufacturing] ...`, the GitHub Search API found 0 matching issues. Consequently, existing roles and apply URLs for the company were not cached, resulting in duplicate job issues being published (e.g., #1264 and #1281).

The goal was to preserve colons in company names while stripping only characters that break GitHub Search phrase query syntax.

## Architecture & Key Decisions
1. **Punctuation Safety in Quoted Phrase Queries**:
   - In GitHub Search syntax, colons only act as field qualifiers outside quotes (e.g. `repo:...`, `is:...`). Inside double-quoted phrase queries (`in:title "[{clean_comp}]"`), colons are treated as literal characters or standard token delimiters.
   - Tested directly against the GitHub Search API: `in:title "[Re:Build Manufacturing]"` successfully matches issues with exit code 0, whereas `in:title "[ReBuild Manufacturing]"` fails to match.
2. **Updated Sanitization Regex**:
   - Replaced `re.sub(r'["\[\]:]', "", company)` with `re.sub(r'["\[\]\\]', "", company)`.
   - Strips double quotes (`"`), brackets (`[` and `]`), and backslashes (`\`) to protect the phrase wrapper while keeping internal punctuation such as colons intact.

## Alternatives Considered & Rejected
1. **Replacing colons with spaces (`Re Build`)**:
   - Rejected because while GitHub search tokenizes `Re:Build` into `Re` and `Build`, keeping the original colon string allows exact string equality in post-search filtering (`issue_comp == company.strip().lower()`).
2. **Exempting specific companies via a hardcoded allowlist**:
   - Rejected as fragile and unmaintainable for arbitrary company names.
