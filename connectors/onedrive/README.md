# OneDrive connector

Microsoft OneDrive through Microsoft Graph, acting as the signed-in user on
their own drive (`/me/drive`). The tools and their parameters are listed in
[docs/TOOLS.md](../../docs/TOOLS.md).

This connector is `blocked` in [publication.json](../publication.json): every
test here fakes Graph at the transport, so nothing in the suite shows how a
real tenant answers. It stays blocked until the checks below have been run.

## Before unblocking

Run each of these against a real OneDrive test account (never a personal
drive), and note the outcome in the pull request that flips the status.

- [ ] **Copy and move to the root.** `onedrive_copy_item` with no
      `destination_folder_id`, and `onedrive_move_item` with
      `destination_folder_id: "root"`, both land in the drive root. The tools
      resolve the root's real id (`GET /me/drive/root`), since Graph refuses
      the `"root"` alias as a destination.
- [ ] **Large upload.** A file over 4 MB goes through an upload session, and
      one fragment interrupted mid-upload resumes.
- [ ] **Conflicts.** `onedrive_upload_file` with `conflict_behavior: "fail"`
      on an existing name returns the 409 message; `"rename"` produces
      `name 1.ext`.
- [ ] **Share links.** `scope: "anonymous"` with `expiry_days` on a business
      tenant whose policy disables anonymous links gives a clear error.
      `scope: "organization"` on a personal (MSA) account, where that scope
      does not exist, gives a clear error rather than a schema failure.
- [ ] **Paging.** `onedrive_list_files` on a folder of more than 200 items,
      following `next_cursor` to the end, returns every item once, with no
      duplicates or gaps.
- [ ] **Search.** `onedrive_search_files` with a term containing `'` and `#`
      returns results.
- [ ] **Consent.** A token without `Files.ReadWrite` gets the 403 message
      that names the fix.
