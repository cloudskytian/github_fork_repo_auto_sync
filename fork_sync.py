import collections
import json
import logging
import os
import shutil
import sys

import github
import git
import requests

LOG_FORMAT = "%(asctime)s | %(filename)s:%(lineno)d | %(levelname)s | %(message)s"
LOG_FILENAME = f"{os.path.basename(__file__)}.log"

logging.basicConfig(format=LOG_FORMAT, level=logging.INFO, filemode="w", filename=LOG_FILENAME)
logger = logging.getLogger()
logger.addHandler(logging.StreamHandler())


def get_remote_refs(url):
    output = git.Git().ls_remote("--heads", "--tags", url)
    tags, branches = {}, {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        sha, ref = parts
        if ref.startswith('refs/heads/'):
            branch_name = ref.removeprefix('refs/heads/')
            branches[branch_name] = sha
        elif ref.startswith('refs/tags/'):
            tag_name = ref.removeprefix('refs/tags/').removesuffix('^{}')
            tags[tag_name] = sha
    return tags, branches


def update_fork_config(gh_token, fork_config_path):
    if os.path.exists(fork_config_path):
        logger.info("loading fork_config.json")
        with open(fork_config_path, "r", encoding="UTF-8-sig") as json_file:
            fork_config = json.load(json_file, object_pairs_hook=collections.OrderedDict)
    else:
        logger.info("fork_config.json not exist, use new")
        fork_config = collections.OrderedDict()
    auth = github.Auth.Token(gh_token)
    g = github.Github(auth=auth)
    user = g.get_user()
    for repo in user.get_repos():
        logger.info(f"{repo.name} | {repo.clone_url}")
        if not repo.name.startswith("zzz_"):
            logger.info(f"{repo.name} | not starts with zzz_ , skip")
            continue
        if repo.fork:
            logger.info(f"{repo.name} | is a fork")
            if repo.name not in fork_config:
                logger.info(f"{repo.name} | not in fork_config, the parent is {repo.parent.full_name}")
                fork_config[repo.name] = collections.OrderedDict()
                fork_config[repo.name]["parent"] = repo.parent.full_name
    with open(fork_config_path, "w", encoding="utf-8") as f:
        json.dump(fork_config, f, ensure_ascii=False, indent=4)


def download_file(url, file_path, headers=None):
    try:
        with requests.get(url, headers=headers, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(file_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
    except Exception:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass
        raise


def fork_sync(gh_token):
    error_flag = False
    warning_list = []
    warning_file_path = "warning_file"
    if os.path.exists(warning_file_path):
        os.remove(warning_file_path)
    fork_config_path = "fork_config.json"
    if os.path.exists(fork_config_path):
        logger.info("loading fork_config.json")
        with open(fork_config_path, "r", encoding="UTF-8-sig") as json_file:
            fork_config = json.load(json_file, object_pairs_hook=collections.OrderedDict)
    else:
        logger.info("fork_config.json not exist, use new")
        fork_config = collections.OrderedDict()
    auth = github.Auth.Token(gh_token)
    g = github.Github(auth=auth)
    user = g.get_user()
    for repo in user.get_repos():
        warning_flag = False
        try:
            old_repo = None
            logger.info(f"{repo.name} | {repo.clone_url}")
            if not repo.name.startswith("zzz_"):
                logger.info(f"{repo.name} | not starts with zzz_ , skip")
                continue
            if repo.fork:
                logger.info(f"{repo.name} | is a fork")
                if repo.name not in fork_config:
                    logger.info(f"{repo.name} | not in fork_config, the parent is {repo.parent.full_name}")
                    fork_config[repo.name] = collections.OrderedDict()
                    fork_config[repo.name]["parent"] = repo.parent.full_name
                logger.info(f"{repo.name} | leave fork network from {repo.parent.full_name}")
                logger.info(f"{repo.name} | rename to old")
                repo_name = repo.name
                repo.edit(name=repo.name + "_old")
                old_repo = repo
                logger.info(f"{repo.name} | create new repo")
                repo = user.create_repo(name=repo_name, private=True, auto_init=False)
            elif repo.name not in fork_config:
                logger.error(f"{repo.name} not in fork_config")
                error_flag = True
                continue
            fork_url = f"https://{gh_token}@github.com/{repo.full_name}.git"
            fork_tags, fork_branches = get_remote_refs(fork_url)
            upstream_url = f'https://github.com/{fork_config[repo.name]["parent"]}.git'
            upstream_tags, upstream_branches = get_remote_refs(upstream_url)
            upstream_repo = g.get_repo(fork_config[repo.name]["parent"])
            logger.info(f"{repo.name} | {upstream_repo.clone_url}")
            resync = False
            fork_config[repo.name]["tags"] = upstream_tags
            fork_config[repo.name]["branches"] = upstream_branches
            if fork_tags != upstream_tags:
                for tag in list(fork_tags.keys()):
                    if tag not in upstream_tags:
                        try:
                            logger.info(f"{repo.name} | fork_tags != upstream_tags, del tag {tag} {fork_tags[tag]}")
                            repo.get_git_ref(f"tags/{tag}").delete()
                            fork_tags.pop(tag)
                        except Exception:
                            pass
            if fork_tags != upstream_tags:
                logger.info(f"{repo.name} | fork_tags != upstream_tags, resync")
                resync = True
            else:
                if fork_branches != upstream_branches:
                    logger.info(f"{repo.name} | fork_branches != upstream_branches, resync")
                    resync = True
            if resync:
                logger.info(f"{repo.name} | disable github action")
                repo._requester.requestJson("PUT", f"{repo.url}/actions/permissions", input={"enabled": False})
                logger.info(f"{repo.name} | clone from {upstream_url}")
                repo_path = "fork_tmp" + repo.name
                if os.path.exists(repo_path):
                    shutil.rmtree(repo_path, ignore_errors=True)
                repo_clone = git.Repo.clone_from(upstream_url, repo_path, bare=True)
                repo_clone.git.lfs("fetch", "--all")
                repo_clone.create_remote("fork", fork_url)
                logger.info(f"{repo.name} | push to fork")
                repo_clone.git.lfs("push", "fork", "--all")
                repo_clone.git.push("fork", "--all", "--force", "--prune")
                repo_clone.git.push("fork", "--tags", "--force")
                if os.path.exists(repo_path):
                    shutil.rmtree(repo_path, ignore_errors=True)
            if repo.default_branch != upstream_repo.default_branch:
                logger.info(f"{repo.name} | set default branch {upstream_repo.default_branch}")
                repo.edit(default_branch=upstream_repo.default_branch)
            if upstream_repo.description and repo.description != upstream_repo.description:
                logger.info(f"{repo.name} | sync description")
                try:
                    if len(upstream_repo.description) > 349:
                        repo.edit(description=upstream_repo.description[:346] + "...")
                    else:
                        repo.edit(description=upstream_repo.description)
                except Exception:
                    logger.warning(f"{repo.name} | warning", exc_info=True)
            if not repo.private:
                logger.info(f"{repo.name} | set to private")
                repo.edit(private=True)
            try:
                fork_latest_release = repo.get_latest_release()
            except Exception:
                fork_latest_release = None
            try:
                upstream_latest_release = upstream_repo.get_latest_release()
            except Exception:
                upstream_latest_release = None
            resync_latest_release = False
            if not upstream_latest_release:
                resync_latest_release = False
            elif resync and not fork_latest_release:
                logger.info(f"{repo.name} | resync and not latest_release, resync_latest_release")
                resync_latest_release = True
            else:
                if upstream_latest_release and not fork_latest_release:
                    logger.info(f"{repo.name} | not latest_release, resync_latest_release")
                    resync_latest_release = True
                elif fork_latest_release.tag_name != upstream_latest_release.tag_name:
                    logger.info(f"{repo.name} | latest_release.tag_name != upstream_latest_release.tag_name, resync_latest_release")
                    resync_latest_release = True
                elif upstream_latest_release.body and fork_latest_release.body != upstream_latest_release.body:
                    logger.info(f"{repo.name} | latest_release.body != upstream_latest_release.body, resync_latest_release")
                    resync_latest_release = True
                elif len(fork_latest_release.assets) != len(upstream_latest_release.assets):
                    if len(upstream_latest_release.assets) == 0:
                        logger.info(f"{repo.name} | upstream has 0 assets but fork has assets, keep fork assets, skip resync")
                    else:
                        logger.info(f"{repo.name} | len(latest_release.assets) != len(upstream_latest_release.assets), resync_latest_release")
                        resync_latest_release = True
                elif upstream_latest_release:
                    upstream_assets = dict()
                    fork_assets = dict()
                    for i in range(0, len(upstream_latest_release.assets)):
                        asset_name = upstream_latest_release.assets[i].name
                        upstream_assets[asset_name] = dict()
                        upstream_assets[asset_name]["size"] = upstream_latest_release.assets[i].size
                        if getattr(upstream_latest_release.assets[i], "digest", None):
                            upstream_assets[asset_name]["hash"] = str(upstream_latest_release.assets[i].digest)
                        else:
                            upstream_assets[asset_name]["hash"] = ""
                    for i in range(0, len(fork_latest_release.assets)):
                        asset_name = fork_latest_release.assets[i].name
                        fork_assets[asset_name] = dict()
                        fork_assets[asset_name]["size"] = fork_latest_release.assets[i].size
                        if getattr(fork_latest_release.assets[i], "digest", None):
                            fork_assets[asset_name]["hash"] = str(fork_latest_release.assets[i].digest)
                        else:
                            fork_assets[asset_name]["hash"] = ""
                    for asset in upstream_assets:
                        if asset not in fork_assets:
                            logger.info(f"{repo.name} | asset {asset} not exist, resync_latest_release")
                            resync_latest_release = True
                            break
                        elif upstream_assets[asset]["hash"] != "":
                            if fork_assets[asset]["hash"] != upstream_assets[asset]["hash"]:
                                logger.info(f'{repo.name} | assets.digest {fork_assets[asset]["hash"]} not equal upstream {upstream_assets[asset]["hash"]}, resync_latest_release')
                                resync_latest_release = True
                                break
                        elif fork_assets[asset]["size"] != upstream_assets[asset]["size"]:
                            logger.info(f'{repo.name} | assets.size {fork_assets[asset]["size"]} not equal upstream {upstream_assets[asset]["size"]}, resync_latest_release')
                            resync_latest_release = True
                            break
            if resync_latest_release:
                downloaded_fork_assets = []
                if fork_latest_release and len(fork_latest_release.assets) and len(upstream_latest_release.assets) == 0:
                    logger.info(f"{repo.name} | len(fork.assets)>0 and len(upstream.assets)==0, downloading assets from fork_latest_release")
                    api_headers = {"Authorization": f"token {gh_token}", "Accept": "application/octet-stream"}
                    for asset in fork_latest_release.assets:
                        logger.info(f"{repo.name} | downloading fork asset {asset.name}")
                        try:
                            download_file(asset.url, asset.name, headers=api_headers)
                        except Exception:
                            download_file(asset.browser_download_url, asset.name)
                        downloaded_fork_assets.append(asset.name)
                try:
                    tag_release = repo.get_release(upstream_latest_release.tag_name)
                except Exception:
                    tag_release = None
                if tag_release:
                    logger.info(f"{repo.name} | release {tag_release} exist, del")
                    tag_release.delete_release()
                fork_tags = dict()
                for tag in repo.get_tags():
                    fork_tags[tag.name] = tag.commit.sha
                if upstream_latest_release.tag_name in fork_tags and fork_tags[upstream_latest_release.tag_name] != upstream_tags[upstream_latest_release.tag_name]:
                    logger.info(
                        f"{repo.name} | tag {upstream_latest_release.tag_name} exist but {fork_tags[upstream_latest_release.tag_name]} not equal {upstream_tags[upstream_latest_release.tag_name]}, resync"
                    )
                    try:
                        repo.get_git_ref(f"tags/{upstream_latest_release.tag_name}").delete()
                    except Exception:
                        pass
                    repo.create_git_ref(f"refs/tags/{upstream_latest_release.tag_name}", upstream_tags[upstream_latest_release.tag_name])
                logger.info(f"{repo.name} | create release")
                if upstream_latest_release.body:
                    message = upstream_latest_release.body
                else:
                    message = ""
                release = repo.create_git_release(draft=upstream_latest_release.draft,
                                                  name=upstream_latest_release.name,
                                                  message=message,
                                                  prerelease=upstream_latest_release.prerelease,
                                                  tag=upstream_latest_release.tag_name)
                if downloaded_fork_assets:
                    for asset_name in downloaded_fork_assets:
                        try:
                            logger.info(f"{repo.name} | uploading fork asset {asset_name}")
                            release.upload_asset(
                                label=asset_name,
                                path=asset_name,
                            )
                        finally:
                            if os.path.exists(asset_name):
                                os.remove(asset_name)
                else:
                    assets = upstream_latest_release.assets
                    if assets:
                        for asset in assets:
                            logger.info(f"{repo.name} | downloading {asset.name}")
                            download_file(asset.browser_download_url, asset.name)
                            try:
                                logger.info(f"{repo.name} | uploading {asset.name}")
                                release.upload_asset(
                                    label=asset.name,
                                    path=asset.name,
                                )
                            finally:
                                if os.path.exists(asset.name):
                                    os.remove(asset.name)
                logger.info(f"{repo.name} | resync_latest_release finish")
            if old_repo:
                logger.info(f"{old_repo.name} | del old")
                old_repo.delete()
        except Exception as e:
            for asset_name in downloaded_fork_assets:
                if os.path.exists(asset_name):
                    try:
                        os.remove(asset_name)
                    except Exception:
                        pass
            if isinstance(e, github.GithubException) or isinstance(e, git.GitCommandError):
                if getattr(e, "status", None) in [403, 404, 451] or (getattr(e, "stderr", None) and "not found" in e.stderr):
                    warning_flag = True
                elif "warning" in fork_config[repo.name]:
                    warning_flag = fork_config[repo.name]["warning"]
            if warning_flag:
                if "warning" not in fork_config[repo.name]:
                    logger.warning(f"{repo.name} | warning", exc_info=True)
                    fork_config[repo.name]["warning"] = True
                    warning_list.append(str(e) + "\n\n")
                continue
            else:
                logger.error(f"{repo.name} | error", exc_info=True)
                error_flag = True
                continue
    with open(fork_config_path, "w", encoding="utf-8") as f:
        json.dump(fork_config, f, ensure_ascii=False, indent=4)
    if len(warning_list) != 0:
        with open(warning_file_path, "w", encoding="utf-8") as f:
            f.writelines(warning_list)
    logger.info("All repos sync finished")
    if error_flag:
        sys.exit(1)


if __name__ == "__main__":
    gh_token = sys.argv[1]
    fork_sync(gh_token)
