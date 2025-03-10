# How a New OVMS Release Is Made

The OVMS integration follows a two-phase release process:

## 1. Create and merge a release Pull Request

A release PR is created to update the version in manifest.json and goes through validation checks.

<details>
<summary>Read more about the PR creation phase</summary>

This phase involves preparing the codebase for release:

1. **Version Update**: The version in `custom_components/ovms/manifest.json` is updated to match the new release version (e.g., "v0.3.24").

2. **Release Branch Creation**: A branch named `release/vX.Y.Z` (e.g., `release/v0.3.24`) is created with this change.

3. **PR Creation**: A pull request is opened using this branch, targeting the main branch.

4. **Automated Validation**: When this PR is created, the `release_test.yml` workflow automatically runs multiple validation checks:
   - HACS validation (compatibility with Home Assistant Community Store)
   - Hassfest validation (compatibility with Home Assistant core)
   - Python code validation (syntax checking, linting)
   - Version check (ensures the manifest version matches the branch name)
   - The PR is automatically labeled with "release"

5. **Manual Review**: The PR must be reviewed and approved according to the checklist in the PR template.

This can be done manually or automated using the `release.sh` script with the `--pr-only` flag:
```bash
bash release.sh v0.3.24 --pr-only
```

This script handles steps 1-3, and GitHub Actions handles step 4.
</details>

## 2. Push a version tag to trigger the release

After the PR is merged, a tag is pushed to create the actual release.

<details>
<summary>Read more about the release creation phase</summary>

This phase creates the actual GitHub release:

1. **Tag Creation**: After the PR is merged to main, a Git tag matching the version (e.g., `v0.3.24`) is created and pushed to GitHub.

2. **Release Workflow Trigger**: When a tag starting with "v" is pushed, the `release.yml` workflow is automatically triggered.

3. **Asset Preparation**: The workflow:
   - Creates a `release` directory
   - Copies only the necessary files into it:
     - `custom_components/ovms/` (the integration code)
     - `README.md` (documentation)
     - `LICENSE` (legal information)

4. **ZIP Creation**: The workflow zips these files into `ovms-home-assistant.zip`, which will be the installable package.

5. **GitHub Release Creation**: Using the GitHub CLI, the workflow:
   - Creates a new GitHub release with the tag name
   - Sets the title to "OVMS Home Assistant vX.Y.Z"
   - Automatically generates release notes based on changes since the last release
   - Attaches the ZIP file as a downloadable asset
   - Marks it as the latest release

This can be done manually with Git commands or using the `release.sh` script without flags:
```bash
bash release.sh v0.3.24
```

The script checks if the version is already updated in manifest.json, creates and pushes the tag, triggering the GitHub Actions workflow to create the actual release.
</details>

## 3. Release completion and verification

The GitHub Actions workflow creates the release with a downloadable ZIP file.

<details>
<summary>Read more about release verification</summary>

Once the release workflow completes:

1. **Release Availability**: A new release appears on the GitHub repository's Releases page with:
   - The version number in the title
   - Automatically generated release notes
   - The `ovms-home-assistant.zip` file attached as a downloadable asset

2. **HACS Discovery**: When properly tagged and released, the new version becomes discoverable in the Home Assistant Community Store (HACS).

3. **Verification Steps**:
   - Check that the release appears on GitHub
   - Verify that the ZIP file contains all necessary components
   - Ensure the version in the manifest.json inside the ZIP matches the release tag
   - The ZIP file structure should match what Home Assistant expects for custom integrations

4. **Installation**: Users can install this release either:
   - Through HACS if the repository is added as a custom repository
   - By manually downloading the ZIP and extracting to their Home Assistant config/custom_components directory

The release workflow is designed to create consistent, properly structured releases that follow Home Assistant's requirements for custom integrations.
</details>

## Common Release Pattern

Most commonly, the release process looks like this:

1. Run `bash release.sh v0.3.24 --pr-only` to create a release PR
2. Review and merge the PR after it passes checks
3. Run `bash release.sh v0.3.24` to create the tag and trigger the release
4. Verify the release appears on GitHub with the proper assets

This pattern separates the preparatory steps from the actual release creation, allowing proper validation and review before publication.
