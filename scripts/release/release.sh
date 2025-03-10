#!/bin/bash
# More robust release script for OVMS Home Assistant

# Don't use set -e as it causes script to exit on any error
# Instead we'll handle errors explicitly

# Colors for better readability
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Integration naming constants
MAIN_NAME="OVMS Home Assistant"
SHORT_NAME="OVMS"
FULL_NAME="Open Vehicle Monitoring System HA"
REPO_NAME="ovms-home-assistant"  # Current repository name

# Debug mode - set to true to enable more detailed output
DEBUG=true

# Debug function
function debug_log {
    if [[ "$DEBUG" == "true" ]]; then
        echo -e "${BLUE}DEBUG:${NC} $1" >&2
    fi
}

# Error function
function error_log {
    echo -e "${RED}ERROR:${NC} $1" >&2
}

# Info function
function info_log {
    echo -e "${YELLOW}INFO:${NC} $1" >&2
}

# Success function 
function success_log {
    echo -e "${GREEN}SUCCESS:${NC} $1" >&2
}

# Check if GitHub CLI is installed
if ! command -v gh &> /dev/null; then
    error_log "GitHub CLI (gh) is not installed."
    echo "Please install it from https://cli.github.com/ and authenticate."
    exit 1
fi

# Check GitHub CLI authentication
debug_log "Checking GitHub CLI authentication..."
if ! gh auth status &> /dev/null; then
    error_log "GitHub CLI is not authenticated. Please run 'gh auth login' first."
    exit 1
fi

# Check if jq is installed - warn but don't exit if missing
if ! command -v jq &> /dev/null; then
    info_log "jq is not installed. Some features may not work correctly."
    info_log "Consider installing jq for better release note generation."
    HAS_JQ=false
else
    HAS_JQ=true
fi

# Display usage information
function show_usage {
    echo -e "${YELLOW}Usage:${NC} bash release.sh <version_tag> [options]"
    echo -e "${YELLOW}Example:${NC} bash release.sh v0.3.1"
    echo ""
    echo "Options:"
    echo "  --pr-only     Create a PR for the release without pushing tags"
    echo "  --help        Show this help message"
    echo ""
    echo "The version tag must follow the format 'vX.Y.Z' where X, Y, Z are numbers."
}

# Validate version tag format with proper semver regex
function validate_version_tag {
    if [[ ! "${1}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9\.]+)?$ ]]; then
        error_log "Invalid version tag format!"
        echo "The version tag must follow the format 'vX.Y.Z' or 'vX.Y.Z-suffix'"
        show_usage
        exit 1
    fi
}

# Check if we're on the main branch
function check_branch {
    local current_branch=$(git rev-parse --abbrev-ref HEAD)
    if [[ "$current_branch" != "main" ]]; then
        error_log "You are not on the main branch!"
        echo "Current branch: $current_branch"
        echo "Please switch to the main branch before creating a release."
        exit 1
    fi
}

# Check for uncommitted changes
function check_uncommitted_changes {
    if ! git diff-index --quiet HEAD --; then
        info_log "You have uncommitted changes."
        read -p "Do you want to continue anyway? (y/n): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo "Aborting release process."
            exit 1
        fi
    fi
}

# Check if tag already exists
function check_tag_exists {
    if git tag -l | grep -q "^${1}$"; then
        error_log "Tag ${1} already exists!"
        echo "Please use a different version tag."
        exit 1
    fi
}

# Find manifest.json in a safer way
function update_manifest {
    local version_tag="$1"
    local manifest_files=$(find "$PWD" -name manifest.json -not -path "*/node_modules/*" -not -path "*/\.*")
    local manifest_count=$(echo "$manifest_files" | wc -l)

    if [[ -z "$manifest_files" ]]; then
        error_log "manifest.json not found!"
        exit 1
    elif [[ "$manifest_count" -gt 1 ]]; then
        info_log "Found multiple manifest.json files:"
        echo "$manifest_files"
        echo "Please specify which one to update:"
        select manifest_file in $manifest_files; do
            if [[ -n "$manifest_file" ]]; then
                break
            fi
        done
    else
        manifest_file="$manifest_files"
    fi

    debug_log "Updating version in: $manifest_file"

    # Check if version is already updated
    local current_version=$(grep -o '"version": *"[^"]*"' "$manifest_file" | cut -d'"' -f4)
    if [[ "$current_version" == "$version_tag" ]]; then
        info_log "Version is already set to ${version_tag} in manifest.json"
        return 0
    fi

    # Use different sed syntax for macOS vs Linux
    if [[ "$(uname)" == "Darwin" ]]; then
        sed -i '' "s|\"version\":.*|\"version\": \"${version_tag}\"|g" "$manifest_file"
    else
        sed -i "s|\"version\":.*|\"version\": \"${version_tag}\"|g" "$manifest_file"
    fi

    success_log "Version updated to ${version_tag} in manifest.json"
    return 1  # Return non-zero to indicate changes were made
}

# Check if any changes were made to files
function check_for_changes {
    if git diff --quiet; then
        return 1  # No changes
    else
        return 0  # Changes detected
    fi
}

# Generate simple release notes when GitHub API fails
function generate_release_notes {
    local version_tag="$1"
    local release_notes_file=$(mktemp)
    
    info_log "Generating release notes for $version_tag..."
    
    # Get the previous tag - ONLY the immediate previous tag
    local prev_tag=$(git describe --abbrev=0 --tags HEAD^ 2>/dev/null || echo "")
    
    # Add header to release notes
    echo "# ${MAIN_NAME} ${version_tag}" > "$release_notes_file"
    echo "" >> "$release_notes_file"
    echo "Released on $(date +'%Y-%m-%d')" >> "$release_notes_file"
    echo "" >> "$release_notes_file"
    
    # Check if there are actual changes
    local change_count=0
    if [[ -n "$prev_tag" ]]; then
        change_count=$(git rev-list --count "$prev_tag..HEAD")
    else
        change_count=$(git rev-list --count HEAD)
    fi
    
    debug_log "Found $change_count changes since previous tag ($prev_tag)"
    
    # If there are no changes, create minimal release notes
    if [[ "$change_count" -eq 0 || "$change_count" -eq 1 ]]; then
        # Only one commit (likely just the version bump)
        echo "## Changes" >> "$release_notes_file"
        echo "" >> "$release_notes_file"
        echo "* Version bump to $version_tag" >> "$release_notes_file"
        echo "" >> "$release_notes_file"
        
        # Add minimal changelog
        if [[ -n "$prev_tag" ]]; then
            echo "## Full Changelog" >> "$release_notes_file"
            echo "[$prev_tag...${version_tag}](https://github.com/enoch85/${REPO_NAME}/compare/${prev_tag}...${version_tag})" >> "$release_notes_file"
        fi
        
        debug_log "Generated minimal release notes (no significant changes)"
        echo "$release_notes_file"
        return
    fi
    
    # There are actual changes, generate meaningful release notes
    echo "## Changes" >> "$release_notes_file"
    echo "" >> "$release_notes_file"
    
    # Try to get meaningful changes using different methods
    local have_changes=false
    
    # 1. Try GitHub PR data if available
    if command -v gh &>/dev/null && gh auth status &>/dev/null 2>&1; then
        debug_log "Attempting to get merged PRs using GitHub CLI"
        
        if [[ -n "$prev_tag" ]]; then
            # Get previous tag commit date
            local prev_date=$(git log -1 --format=%aI "$prev_tag")
            debug_log "Previous tag ($prev_tag) date: $prev_date"
            
            # Get and process PRs merged since previous tag
            if prs=$(gh pr list --state merged --base main --json number,title,mergedAt --limit 15 2>/dev/null); then
                debug_log "Successfully retrieved PRs from GitHub"
                
                # Filter to PRs merged after previous tag
                if [[ "$(command -v jq)" && -n "$prs" && "$prs" != "[]" ]]; then
                    # Use jq if available
                    local filtered_prs=$(echo "$prs" | jq -r --arg date "$prev_date" '.[] | select(.mergedAt > $date) | "* " + .title + " (#" + (.number|tostring) + ")"')
                    
                    if [[ -n "$filtered_prs" ]]; then
                        echo "$filtered_prs" >> "$release_notes_file"
                        have_changes=true
                        debug_log "Added PR information to release notes"
                    fi
                fi
            else
                debug_log "Could not retrieve PRs from GitHub"
            fi
        fi
    else
        debug_log "GitHub CLI not available or not authenticated"
    fi
    
    # 2. If we don't have changes yet, use git log to find meaningful commits
    if [[ "$have_changes" != "true" ]]; then
        debug_log "Using git log to find meaningful commits"
        
        # Get non-merge commits since previous tag (or all if no previous tag)
        local commits
        if [[ -n "$prev_tag" ]]; then
            commits=$(git log --no-merges --pretty=format:"* %s (%h)" "$prev_tag..HEAD")
        else
            commits=$(git log --no-merges --pretty=format:"* %s (%h)" -n 10)
        fi
        
        # Filter out automated version bump commits
        local meaningful_commits=$(echo "$commits" | grep -v "Release.*of.*${MAIN_NAME}" | grep -v "Bump version" | head -n 10)
        
        if [[ -n "$meaningful_commits" ]]; then
            echo "$meaningful_commits" >> "$release_notes_file"
            have_changes=true
            debug_log "Added commits to release notes"
        fi
    fi
    
    # 3. If still no changes found, add a fallback message
    if [[ "$have_changes" != "true" ]]; then
        echo "* Minor updates and improvements" >> "$release_notes_file"
        debug_log "No specific changes found, using generic message"
    fi
    
    # Add changelog section
    echo "" >> "$release_notes_file"
    echo "## Full Changelog" >> "$release_notes_file"
    
    if [[ -n "$prev_tag" ]]; then
        echo "[$prev_tag...${version_tag}](https://github.com/enoch85/${REPO_NAME}/compare/${prev_tag}...${version_tag})" >> "$release_notes_file"
    else
        echo "[$version_tag](https://github.com/enoch85/${REPO_NAME}/releases/tag/${version_tag})" >> "$release_notes_file"
    fi
    
    debug_log "Release notes generated successfully"
    echo "$release_notes_file"
}

# Create a PR for the release
function create_release_pr {
    local version_tag="$1"
    local branch_name="release/${version_tag}"
    local release_notes_file="$2"
    
    info_log "Creating release branch ${branch_name}..."
    
    # Create branch
    if ! git checkout -b "$branch_name"; then
        error_log "Failed to create branch $branch_name"
        return 1
    fi
    
    # Commit changes
    debug_log "Adding changes to git"
    if ! git add -A; then
        error_log "Failed to add changes to git"
        return 1
    fi
    
    debug_log "Committing changes"
    if ! git commit -m "Release ${version_tag}"; then
        error_log "Failed to commit changes"
        return 1
    fi
    
    # Push branch
    debug_log "Pushing branch to GitHub"
    if ! git push -u origin "$branch_name"; then
        error_log "Failed to push branch to GitHub"
        return 1
    fi
    
    success_log "Release branch pushed"
    
    # Create PR using GitHub CLI
    info_log "Creating pull request..."
    
    # Use the release notes as PR description
    if pr_url=$(gh pr create --base main --head "$branch_name" --title "${MAIN_NAME} ${version_tag}" --body-file "$release_notes_file"); then
        success_log "Release PR created: ${pr_url}"
    else
        error_log "Failed to create PR automatically. Please create it manually."
        echo "Branch: $branch_name"
        echo "Base: main"
        echo "Title: ${MAIN_NAME} ${version_tag}"
        return 1
    fi
    
    # Cleanup
    debug_log "Switching back to main branch"
    git checkout main
    
    echo -e "${BLUE}Instructions:${NC}"
    echo "1. Review the PR: $pr_url"
    echo "2. Make any necessary changes to the release branch"
    echo "3. Once approved, merge the PR"
    echo "4. Run this script again without --pr-only to push the tag and create a release"
    
    return 0
}

# Create a GitHub release notice (modified to skip creating actual release)
function create_github_release {
    local version_tag="$1"
    local release_notes_file="$2"
    
    info_log "Skipping GitHub release creation - will be handled by GitHub Actions..."
    echo -e "${BLUE}Release notes that will be used for GitHub Actions:${NC}"
    cat "$release_notes_file"
    echo
    success_log "GitHub release will be created automatically by GitHub Actions when tag is pushed"
    echo -e "${BLUE}If you want to review the release after it's created, visit:${NC}"
    echo "https://github.com/enoch85/${REPO_NAME}/releases/tag/${version_tag}"
}

# Creates and pushes a tag
function create_and_push_tag {
    local version_tag="$1"
    
    info_log "Creating and pushing tag ${version_tag}..."
    if ! git tag "${version_tag}"; then
        error_log "Failed to create tag ${version_tag}"
        return 1
    fi
    
    if ! git push origin "${version_tag}"; then
        error_log "Failed to push tag ${version_tag}"
        return 1
    fi
    
    success_log "Tag ${version_tag} created and pushed"
    return 0
}

# Main execution starts here

# Parse arguments
PR_ONLY=false

while [[ "$#" -gt 0 ]]; do
    case $1 in
        --pr-only) PR_ONLY=true; shift ;;
        --help) show_usage; exit 0 ;;
        -h) show_usage; exit 0 ;;
        -*) echo "Unknown option: $1"; show_usage; exit 1 ;;
        *) VERSION_TAG="$1"; shift ;;
    esac
done

# Check if version tag is provided
if [ -z "${VERSION_TAG}" ]; then
    error_log "You forgot to add a release tag!"
    show_usage
    exit 1
fi

validate_version_tag "$VERSION_TAG"
check_branch
check_uncommitted_changes
check_tag_exists "$VERSION_TAG"

info_log "Starting release process for ${MAIN_NAME} ${VERSION_TAG}..."

# Pull latest changes
info_log "Pulling latest changes..."
if ! git pull --rebase; then
    error_log "Failed to pull latest changes."
    echo "Please fix the error, then try again."
    exit 1
fi
success_log "Latest changes pulled"

# Update manifest.json - returns 0 if already updated, 1 if changes were made
update_manifest "$VERSION_TAG"
changes_made=$?
debug_log "Manifest update result: $changes_made (1=changes made, 0=no changes)"

# Generate release notes
debug_log "Generating release notes..."
RELEASE_NOTES_FILE=$(generate_release_notes "$VERSION_TAG")
debug_log "Release notes generated at $RELEASE_NOTES_FILE"

# Create either a PR or a full release
if [[ "$PR_ONLY" == true ]]; then
    info_log "Creating PR only (no tag push)..."
    
    if ! create_release_pr "$VERSION_TAG" "$RELEASE_NOTES_FILE"; then
        error_log "Failed to create PR. Please check the errors above."
        exit 1
    fi
else
    # Check if there are any changes to commit
    if [[ "$changes_made" -eq 1 ]] || check_for_changes; then
        # Stage files
        info_log "Staging changes..."
        git add -A
        success_log "Changes staged"

        # Show summary of changes
        info_log "Summary of changes to be committed:"
        git status --short

        # Display release notes
        info_log "Release Notes:"
        cat "$RELEASE_NOTES_FILE"
        echo

        # Confirm commit
        read -p "Do you want to proceed with the commit, push, and release? (y/n): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo "Aborting release process."
            exit 1
        fi

        # Commit changes
        info_log "Committing changes..."
        if ! git commit -m "Release ${VERSION_TAG} of ${MAIN_NAME}"; then
            error_log "Failed to commit changes"
            exit 1
        fi
        success_log "Changes committed"

        # Push to main
        info_log "Pushing to main branch..."
        if ! git push origin main; then
            error_log "Failed to push changes to main"
            exit 1
        fi
        success_log "Changes pushed to main"

        # Create and push tag
        if ! create_and_push_tag "$VERSION_TAG"; then
            error_log "Failed to create and push tag"
            exit 1
        fi
    else
        info_log "No changes to commit. Manifest.json already has version ${VERSION_TAG}."
        
        # Display release notes
        info_log "Release Notes:"
        cat "$RELEASE_NOTES_FILE"
        echo
        
        # Confirm tag creation
        read -p "Do you want to create and push the tag ${VERSION_TAG}? (y/n): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo "Aborting release process."
            exit 1
        fi
        
        # Create and push tag
        if ! create_and_push_tag "$VERSION_TAG"; then
            error_log "Failed to create and push tag"
            exit 1
        fi
    fi

    # Create GitHub release (now just displays a notice)
    create_github_release "$VERSION_TAG" "$RELEASE_NOTES_FILE"

    success_log "${MAIN_NAME} ${VERSION_TAG} successfully prepared!"
    echo -e "${BLUE}The GitHub Actions workflow will now create the actual release.${NC}"
    echo -e "${BLUE}You can monitor the process at:${NC} https://github.com/enoch85/${REPO_NAME}/actions"
fi

# Clean up temporary file
if [[ -f "$RELEASE_NOTES_FILE" ]]; then
    debug_log "Cleaning up temporary release notes file"
    rm "$RELEASE_NOTES_FILE"
fi

success_log "Release script completed successfully."
