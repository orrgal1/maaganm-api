import json
import subprocess
import sys
import time

REPOSITORY = "orrgal1/maaganm-api"


def _gh_api(endpoint):
    process = subprocess.run(
        ["gh", "api", endpoint],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if process.returncode != 0:
        raise RuntimeError("GitHub API request failed")
    return json.loads(process.stdout)


def get_issues_and_comments():
    issues = _gh_api(
        f"repos/{REPOSITORY}/issues?state=all&sort=created&direction=desc&per_page=100"
    )
    comments = _gh_api(
        f"repos/{REPOSITORY}/issues/comments?sort=created&direction=desc&per_page=100"
    )
    return issues, comments


def watch(interval=15):
    known_issues = set()
    known_comments = set()
    initialized = False

    while True:
        try:
            issues, comments = get_issues_and_comments()
            issue_ids = {issue["id"] for issue in issues}
            comment_ids = {comment["id"] for comment in comments}

            if initialized:
                for issue in reversed(issues):
                    if issue["id"] not in known_issues:
                        print(
                            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                            f"New issue #{issue['number']}: {issue['title']} "
                            f"{issue['html_url']}",
                            flush=True,
                        )
                for comment in reversed(comments):
                    if comment["id"] not in known_comments:
                        author = comment.get("user", {}).get("login", "unknown")
                        summary = " ".join(comment.get("body", "").split())[:240]
                        print(
                            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                            f"New comment by {author}: {summary} "
                            f"{comment['html_url']}",
                            flush=True,
                        )
            else:
                print(
                    f"READY watching {REPOSITORY}: "
                    f"{len(issues)} recent issues, {len(comments)} recent comments",
                    flush=True,
                )
                initialized = True

            known_issues.update(issue_ids)
            known_comments.update(comment_ids)
        except (json.JSONDecodeError, RuntimeError, subprocess.TimeoutExpired) as error:
            print(f"GitHub polling error: {error}", file=sys.stderr, flush=True)

        time.sleep(interval)


if __name__ == "__main__":
    watch(int(sys.argv[1]) if len(sys.argv) > 1 else 15)
