"""Primary API for repomate.

This module contains high level functions for administrating repositories, such
as creating student repos from some master repo template. All functions follow
the conventions specified in :ref:`conventions`.

Each public function in this module is to be treated as a self-contained
program.

.. module:: command
    :synopsis: The primary API of repomate containing high level functions for
        administrating GitHub repos in an opinionated fashion.

.. moduleauthor:: Simon Larsén
"""

import os
import sys
import tempfile
from typing import Iterable, List, Optional, Tuple, Generator
from colored import bg, fg, style

import daiquiri

import repomate_plug as plug

from repomate import git
from repomate import util
from repomate import tuples
from repomate import exception
from repomate import config
from repomate import formatters
from repomate.github_api import GitHubAPI
from repomate.tuples import Team
from repomate.git import Push

LOGGER = daiquiri.getLogger(__file__)

MASTER_TEAM = "master_repos"


def setup_student_repos(
    master_repo_urls: Iterable[str],
    students: Iterable[str],
    user: str,
    api: GitHubAPI,
) -> None:
    """Setup student repositories based on master repo templates. Performs three
    primary tasks:

        1. Create one team per student and add the corresponding students to
        their teams. If a team already exists, it is left as-is. If a student
        is already in its team, nothing happens. If no account exists with the
        specified username, the team is created regardless but no one is added
        to it.

        2. For each master repository, create one student repo per team and add
        it to the corresponding student team. If a repository already exists,
        it is skipped.

        3. Push files from the master repos to the corresponding student repos.

    Args:
        master_repo_urls: URLs to master repos. Must be in the organization
        that the api is set up for.
        students: An iterable of student GitHub usernames.
        user: Username of the administrator that setting up the repos.
        api: A GitHubAPI instance used to interface with the GitHub instance.
    """
    util.validate_types(user=(user, str), api=(api, GitHubAPI))
    util.validate_non_empty(
        master_repo_urls=master_repo_urls, students=students, user=user
    )

    urls = list(master_repo_urls)  # safe copy

    with tempfile.TemporaryDirectory() as tmpdir:
        LOGGER.info("cloning into master repos ...")
        master_repo_paths = _clone_all(urls, api.token, cwd=tmpdir)

        teams = add_students_to_teams(students, api)
        repo_urls = _create_student_repos(urls, teams, api)

        push_tuples = _create_push_tuples(master_repo_paths, repo_urls)
        LOGGER.info("pushing files to student repos ...")
        git.push(push_tuples, user=user, token=api.token)


def add_students_to_teams(
    students: Iterable[str], api: GitHubAPI
) -> List[Team]:
    """Create one team for each student (with the same name as the student),
    and add the student to the team. If a team already exists, it is not
    created.  If a student is already in his/her team, nothing happens.

    Args:
        students: Student GitHub usernames.
        api: A GitHubAPI instance.

    Returns:
        all teams associated with the students in the students list.
    """
    util.validate_types(api=(api, GitHubAPI))
    util.validate_non_empty(students=students)
    # (team_name, member list) mappings, each student gets its own team
    member_lists = {student: [student] for student in students}
    return api.ensure_teams_and_members(member_lists)


def _create_student_repos(
    master_repo_urls: Iterable[str], teams: Iterable[Team], api: GitHubAPI
) -> List[str]:
    """Create student repos. Each team (usually representing one student) is
    assigned a single repo per master repo. Repos that already exist are not
    created, but their urls are returned all the same.

    Args:
        master_repo_urls: URLs to master repos. Must be in the organization
            that the api is set up for.
        teams: An iterable of namedtuples designating different teams.
        api: A GitHubAPI instance used to interface with the GitHub instance.

    Returns:
        a list of urls to the repos
    """
    LOGGER.info("creating student repos ...")
    repo_infos = _create_repo_infos(master_repo_urls, teams)
    repo_urls = api.create_repos(repo_infos)
    return repo_urls


def _clone_all(urls: Iterable[str], token: str, cwd: str):
    """Attempts to clone all urls. If a repo is already present, it is skipped.
    If any one clone fails (except for fails because the repo is local),
    all cloned repos are removed

    Args:
        urls: HTTPS urls to git repositories.
        token: A GitHub OATH token.
        cwd: Working directory. Use temporary directory for automatic cleanup.
    Returns:
        local paths to the cloned repos.
    """
    if len(set(urls)) != len(urls):
        raise ValueError("master_repo_urls contains duplicates")
    try:
        for url in urls:
            LOGGER.info("cloning into {}".format(url))
            git.clone_single(url, token, cwd=cwd)
    except exception.CloneFailedError:
        LOGGER.error("error cloning into {}, aborting ...".format(url))
        raise
    paths = [os.path.join(cwd, util.repo_name(url)) for url in urls]
    assert all(map(util.is_git_repo, paths)), "all repos must be git repos"
    return paths


def update_student_repos(
    master_repo_urls: Iterable[str],
    students: Iterable[str],
    user: str,
    api: GitHubAPI,
    issue: Optional[tuples.Issue] = None,
) -> None:
    """Attempt to update all student repos related to one of the master repos.

    Args:
        master_repo_urls: URLs to master repos. Must be in the organization
            that the api is set up for.
        students: An iterable of student GitHub usernames.
        user: Username of the administrator that setting up the repos.
        api: A GitHubAPI instance used to interface with the GitHub instance.
        issue: An optional issue to open in repos to which pushing fails.
    """
    util.validate_types(
        user=(user, str),
        api=(api, GitHubAPI),
        issue=(issue, (tuples.Issue, type(None))),
    )
    util.validate_non_empty(
        master_repo_urls=master_repo_urls, user=user, students=students
    )
    urls = list(master_repo_urls)  # safe copy

    if len(set(urls)) != len(urls):
        raise ValueError("master_repo_urls contains duplicates")

    master_repo_names = [util.repo_name(url) for url in urls]
    student_repo_names = util.generate_repo_names(students, master_repo_names)

    repo_urls = api.get_repo_urls(student_repo_names)

    with tempfile.TemporaryDirectory() as tmpdir:
        LOGGER.info("cloning into master repos ...")
        master_repo_paths = _clone_all(urls, api.token, tmpdir)

        push_tuples = _create_push_tuples(master_repo_paths, repo_urls)

        LOGGER.info("pushing files to student repos ...")
        failed_urls = git.push(push_tuples, user=user, token=api.token)

    if failed_urls and issue:
        LOGGER.info("Opening issue in repos to which push failed")
        _open_issue_by_urls(failed_urls, issue, api)

    LOGGER.info("done!")


def _open_issue_by_urls(
    repo_urls: Iterable[str], issue: tuples.Issue, api: GitHubAPI
) -> None:
    """Open issues in the repos designated by the repo_urls.

    repo_urls: URLs to repos in which to open an issue.
    issue: An issue to open.
    api: A GitHubAPI to use.
    """
    repo_names = [util.repo_name(url) for url in repo_urls]
    api.open_issue(issue.title, issue.body, repo_names)


def list_issues(
    master_repo_names: Iterable[str],
    students: Iterable[str],
    api: GitHubAPI,
    state: str = "open",
    title_regex: str = "",
    show_body: bool = False,
    author: Optional[str] = None,
) -> None:
    """List all issues in the specified repos.

    Args:
        master_repo_names: Names of master repositories.
        students: An iterable of student GitHub usernames.
        state: state of the repo (open or closed). Defaults to 'open'.
        api: A GitHubAPI instance used to interface with the GitHub instance.
        title_regex: If specified, only issues with titles matching the regex
        are displayed. Defaults to the empty string (which matches everything).
        show_body: If True, the body of the issue is displayed along with the
        default info.
        author: Only show issues by this author.
    """
    util.validate_types(api=(api, GitHubAPI))
    util.validate_non_empty(
        master_repo_names=master_repo_names, students=students
    )

    repo_names = util.generate_repo_names(students, master_repo_names)
    max_repo_name_length = max(map(len, repo_names))

    issues_per_repo = api.get_issues(repo_names, state, title_regex)

    if author:
        issues_per_repo = (
            (
                repo_name,
                (issue for issue in issues if issue.user.login == author),
            )
            for repo_name, issues in issues_per_repo
        )

    _log_repo_issues(issues_per_repo, show_body, max_repo_name_length + 6)


def _log_repo_issues(
    issues_per_repo: Tuple[str, Generator[tuples.Issue, None, None]],
    show_body: bool,
    title_alignment: int,
) -> None:
    """Log repo issues.

    Args:
        issues_per_repo: (repo_name, issue generator) pairs
        show_body: Include the body of the issue in the output.
        title_alignment: Where the issue title should start counting from the
        start of the line.
    """
    even = True
    for repo_name, issues in issues_per_repo:
        issues = list(issues)

        if not issues:
            LOGGER.warning("{}: No matching issues".format(repo_name))

        for issue in issues:
            color = (bg("grey_30") if even else bg("grey_15")) + fg("white")
            even = not even  # cycle color
            adjusted_alignment = title_alignment + len(
                color
            )  # color takes character space

            id_ = "{}{}/#{}:".format(color, repo_name, issue.number).ljust(
                adjusted_alignment
            )
            out = "{}{}{}{}created {!s} by {}".format(
                id_,
                issue.title,
                style.RESET,
                " ",
                issue.created_at,
                issue.user.login,
            )
            if show_body:
                out += os.linesep * 2 + _limit_line_length(issue.body)
            LOGGER.info(out)


def _limit_line_length(s: str, max_line_length: int = 100) -> str:
    """Return the input string with lines no longer than max_line_length.

    Args:
        s: Any string.
        max_line_length: Maximum allowed line length.
    Returns:
        the input string with lines no longer than max_line_length.
    """
    lines = s.split(os.linesep)
    out = ""
    for line in lines:
        cur = 0
        while len(line) - cur > max_line_length:
            # find ws closest to the line length
            idx = line.rfind(" ", cur, max_line_length + cur)
            idx = max_line_length + cur if idx <= 0 else idx
            if line[idx] == " ":
                out += line[cur:idx]
            else:
                out += line[cur : idx + 1]
            out += os.linesep
            cur = idx + 1
        out += line[cur : cur + max_line_length] + os.linesep
    return out


def open_issue(
    issue: tuples.Issue,
    master_repo_names: Iterable[str],
    students: Iterable[str],
    api: GitHubAPI,
) -> None:
    """Open an issue in student repos.

    Args:
        master_repo_names: Names of master repositories.
        students: An iterable of student GitHub usernames.
        issue: An issue to open.
        api: A GitHubAPI instance used to interface with the GitHub instance.
    """
    util.validate_types(issue=(issue, tuples.Issue), api=(api, GitHubAPI))
    util.validate_non_empty(
        master_repo_names=master_repo_names, students=students, issue=issue
    )

    repo_names = util.generate_repo_names(students, master_repo_names)

    api.open_issue(issue.title, issue.body, repo_names)


def close_issue(
    title_regex: str,
    master_repo_names: Iterable[str],
    students: Iterable[str],
    api: GitHubAPI,
) -> None:
    """Close issues whose titles match the title_regex in student repos.

    Args:
        title_regex: A regex to match against issue titles.
        master_repo_names: Names of master repositories.
        students: An iterable of student GitHub usernames.
        api: A GitHubAPI instance used to interface with the GitHub instance.
    """
    util.validate_types(title_regex=(title_regex, str), api=(api, GitHubAPI))
    util.validate_non_empty(
        title_regex=title_regex,
        master_repo_names=master_repo_names,
        students=students,
    )

    repo_names = util.generate_repo_names(students, master_repo_names)

    api.close_issue(title_regex, repo_names)


def clone_repos(
    master_repo_names: Iterable[str], students: Iterable[str], api: GitHubAPI
) -> None:
    """Clone all student repos related to the provided master repos and students.

    Args:
        master_repo_names: Names of master repos.
        students: Student usernames.
        api: A GitHubAPI instance.
    """
    util.validate_types(api=(api, GitHubAPI))
    util.validate_non_empty(
        master_repo_names=master_repo_names, students=students
    )

    repo_names = util.generate_repo_names(students, master_repo_names)
    repo_urls = api.get_repo_urls(repo_names)

    LOGGER.info("cloning into student repos ...")
    git.clone(repo_urls, api.token)

    if (
        len(plug.manager.get_plugins()) > 1
    ):  # something else than the default loaded
        _execute_post_clone_hooks(repo_names, api)


def _execute_post_clone_hooks(repo_names: List[str], api: GitHubAPI):
    LOGGER.info("executing post clone hooks on repos")
    local_repos = [name for name in os.listdir() if name in repo_names]

    results = {}
    for repo_name in local_repos:
        LOGGER.info("executing post clone hooks on {}".format(repo_name))
        res = plug.manager.hook.act_on_cloned_repo(
            path=os.path.abspath(repo_name), api=api
        )
        results[repo_name] = res
    LOGGER.info(formatters.format_hook_results_output(results))

    LOGGER.info("post clone hooks done")


def migrate_repos(
    master_repo_urls: Iterable[str], user: str, api: GitHubAPI
) -> None:
    """Migrate a repository from an arbitrary URL to the target organization.
    The new repository is added to the master_repos team, which is created if
    it does not already exist.

    Args:
        master_repo_urls: HTTPS URLs to the master repos to migrate.
        user: username of the administrator performing the migration. This is
        the username that is used in the push.
        api: A GitHubAPI instance used to interface with the GitHub instance.
    """
    util.validate_types(user=(user, str), api=(api, GitHubAPI))
    util.validate_non_empty(master_repo_urls=master_repo_urls, user=user)

    master_team, *_ = api.ensure_teams_and_members({MASTER_TEAM: []})

    master_names = [util.repo_name(url) for url in master_repo_urls]

    infos = [
        tuples.Repo(
            name=master_name,
            description="Master repository {}".format(master_name),
            private=True,
            team_id=master_team.id,
        )
        for master_name in master_names
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        _clone_all(master_repo_urls, api.token, cwd=tmpdir)
        repo_urls = api.create_repos(infos)

        git.push(
            [
                git.Push(
                    local_path=os.path.join(tmpdir, info.name),
                    repo_url=repo_url,
                    branch="master",
                )
                for repo_url, info in zip(repo_urls, infos)
            ],
            user=user,
            token=api.token,
        )

    LOGGER.info("done!")


def assign_peer_reviews(
    master_repo_names: Iterable[str],
    students: Iterable[str],
    num_reviews: int,
    issue: Optional[tuples.Issue],
    api: GitHubAPI,
) -> None:
    """Assign peer reviewers among the students to each student repo. Each
    student is assigned to review num_reviews repos, and consequently, each
    repo gets reviewed by num_reviews reviewers.

    In practice, each student repo has a review team generated (called
    <student-repo-name>-review), to which num_reviews _other_ students are
    assigned. The team itself is given pull-access to the student repo, so
    that reviewers can view code and open issues, but cannot modify the
    contents of the repo.

    Args:
        master_repo_names: Names of master repos.
        students: An iterable of student GitHub usernames.
        num_reviews: Amount of reviews each student should perform
            (consequently, the amount of reviews of each repo)
        api: A GitHubAPI instance used to interface with the GitHub instance.
    """
    util.validate_types(
        api=(api, GitHubAPI), issue=(issue, (tuples.Issue, type(None)))
    )
    util.validate_non_empty(
        master_repo_names=master_repo_names, students=students
    )

    for master_name in master_repo_names:
        allocations = plug.manager.hook.generate_review_allocations(
            master_repo_name=master_name,
            students=students,
            num_reviews=num_reviews,
            review_team_name_function=util.generate_review_team_name,
        )
        api.ensure_teams_and_members(allocations, permission="pull")
        api.add_repos_to_review_teams(
            {
                util.generate_review_team_name(student, master_name): [
                    util.generate_repo_name(student, master_name)
                ]
                for student in students
            },
            issue=issue,
        )


def purge_review_teams(
    master_repo_names: Iterable[str], students: Iterable[str], api: GitHubAPI
) -> None:
    """Delete all review teams associated with the given master repo names and
    students.

    Args:
        master_repo_names: Names of master repos.
        students: An iterable of student GitHub usernames.
    """
    util.validate_non_empty(
        master_repo_names=master_repo_names, students=students
    )

    review_team_names = [
        util.generate_review_team_name(student, master_repo_name)
        for student in students
        for master_repo_name in master_repo_names
    ]
    api.delete_teams(review_team_names)


def check_peer_review_progress(
    master_repo_names: Iterable[str],
    students: Iterable[str],
    title_regex: str,
    num_reviews: int,
    api: GitHubAPI,
) -> None:
    review_team_names = [
        util.generate_review_team_name(student, master_name)
        for student in students
        for master_name in master_repo_names
    ]
    reviews = api.get_review_progress(review_team_names, students, title_regex)

    LOGGER.info(
        formatters.format_peer_review_progress_output(
            reviews, students, num_reviews
        )
    )


def _create_repo_infos(
    urls: Iterable[str], teams: Iterable[Team]
) -> List[tuples.Repo]:
    """Create Repo namedtuples for all combinations of url and team.

    Args:
        urls: Master repo urls.
        teams: Team namedtuples.

    Returns:
        A list of Repo namedtuples with all (url, team) combinations.
    """
    repo_infos = []
    for url in urls:
        repo_base_name = util.repo_name(url)
        repo_infos += [
            tuples.Repo(
                name=util.generate_repo_name(team.name, repo_base_name),
                description="{} created for {}".format(
                    repo_base_name, team.name
                ),
                private=True,
                team_id=team.id,
            )
            for team in teams
        ]
    return repo_infos


def _create_push_tuples(
    master_repo_paths: Iterable[str], repo_urls: Iterable[str]
) -> List[Push]:
    """Create Push namedtuples for all repo urls in repo_urls that share
    repo base name with any of the urls in master_urls.

    Args:
        master_repo_paths: Local paths to master repos.
        repo_urls: Urls to student repos.

    Returns:
        A list of Push namedtuples for all student repo urls that relate to
        any of the master repo urls.
    """
    push_tuples = []
    for path in master_repo_paths:
        repo_base_name = os.path.basename(path)
        push_tuples += [
            git.Push(local_path=path, repo_url=repo_url, branch="master")
            for repo_url in repo_urls
            if repo_url.endswith(repo_base_name)
        ]
    return push_tuples


def show_config():
    """Print the configuration file to the log."""
    config.check_config_integrity()

    LOGGER.info(
        "found valid config file at " + str(config.DEFAULT_CONFIG_FILE)
    )
    with config.DEFAULT_CONFIG_FILE.open(
        encoding=sys.getdefaultencoding()
    ) as f:
        config_contents = "".join(f.readlines())

    output = (
        os.linesep
        + "BEGIN CONFIG FILE".center(50, "-")
        + os.linesep
        + config_contents
        + "END CONFIG FILE".center(50, "-")
    )

    LOGGER.info(output)
