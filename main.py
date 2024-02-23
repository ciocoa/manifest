import datetime
import logging
import os
import subprocess
import sys
import time
import winreg
from argparse import ArgumentParser
from multiprocessing.dummy import Pool, Lock
from multiprocessing.pool import ThreadPool
from pathlib import Path

import httpx
import vdf
from colorama import init, Fore
from colorlog import ColoredFormatter
from retrying import retry


def show_banner():
    print(rf'''
    ________  ___  ________  ________  ________     
    |\   ____\|\  \|\   __  \|\   ____\|\   __  \    
    \ \  \___|\ \  \ \  \|\  \ \  \___|\ \  \|\  \   
     \ \  \    \ \  \ \  \\\  \ \  \    \ \  \\\  \  
      \ \  \____\ \  \ \  \\\  \ \  \____\ \  \\\  \ 
       \ \_______\ \__\ \_______\ \_______\ \_______\
        \|_______|\|__|\|_______|\|_______|\|__{version}__|
    ''')


def init_logger():
    logger = logging.getLogger(__name__)
    level = logging.DEBUG if args.debug else logging.INFO
    logger.setLevel(level)
    fmt = '%(log_color)s %(asctime)s [%(levelname)s] %(message)s'
    formatter = ColoredFormatter(fmt)
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


def init_args():
    parser = ArgumentParser()
    parser.add_argument('-v', '--version', action='version', version=f'%(prog)s v{version}')
    parser.add_argument('-a', '--appid', help='steam appid')
    parser.add_argument('-k', '--key', help='github API key')
    parser.add_argument('-r', '--repo', help='github repo name')
    parser.add_argument('-f', '--fixed', action='store_true', help='fixed manifest')
    parser.add_argument('-d', '--debug', action='store_true', help='debug mode')
    return parser.parse_args()


def init_repos():
    repo_list = ['Onekey-Project/ManifestAutoUpdate-Cache', 'ciocoa/manifest']
    if args.repo:
        repo_list.insert(0, args.repo)
    log.debug(f'已加载参数: {args}')
    log.debug(f'已加载仓库: {repo_list}')
    return repo_list


def check_steam_path():
    try:
        hkey = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam')
        steam_path = Path(winreg.QueryValueEx(hkey, 'SteamPath')[0])
        if 'steam.exe' in os.listdir(steam_path):
            log.info(f'检测到Steam: {steam_path}')
            return steam_path
    except Exception as e:
        log.debug(e)


def check_stool_path(steam_path: Path):
    try:
        lua_path = steam_path / 'config' / 'stplug-in'
        stool_path = steam_path / 'config' / 'stUI'
        is_lua = 'luapacka.exe' in os.listdir(lua_path)
        is_stool = 'Steamtools.exe' in os.listdir(stool_path)
        if is_lua and is_stool:
            log.info(f'检测到Stool: {stool_path}')
            return lua_path, stool_path
    except Exception as e:
        log.debug(e)


def check_api_limit():
    limit_res: dict = api_request('https://api.github.com/rate_limit')
    reset = limit_res['rate']['reset']
    remaining = limit_res['rate']['remaining']
    log.info(f'剩余请求次数: {remaining}')
    reset_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(reset))
    if remaining == 0:
        return reset_time


def check_curr_repo():
    last_date = None
    curr_repo = None
    for repo in repos:
        branch_res: dict = api_request(f'https://api.github.com/repos/{repo}/branches/{appid}')
        if not (branch_res is None) and 'commit' in branch_res:
            date = branch_res['commit']['commit']['committer']['date']
            if last_date is None or date > last_date:
                last_date = date
                curr_repo = repo
    log.info(f'当前清单仓库: {curr_repo}')
    return curr_repo


def set_stool(steam_path: Path, lua_filename: str, lua_content: str):
    lua_filepath = steam_path / 'config' / 'stplug-in' / lua_filename
    with open(lua_filepath, 'w') as f:
        f.write(lua_content)
    lua_packpath = steam_path / 'config' / 'stplug-in' / 'luapacka.exe'
    result = subprocess.run([str(lua_packpath), str(lua_filepath)], stdout=subprocess.PIPE)
    if not args.debug:
        os.remove(lua_filepath)
    output = result.stdout.decode('utf-8').removesuffix('\r\n')
    return output[output.index('to ') + 3:]


def set_appinfo(branch: str, depot_list: list, steam_path: Path, is_dlc=False):
    lua_content = ''
    for depot_id, depot_key in depot_list:
        lua_content += f'addappid({depot_id}, 1, "{depot_key}")\n' if depot_key else f'addappid({depot_id}, 1)\n'
    lua_filename = f'{branch}_{'D' if is_dlc else 'A'}.lua'
    out_info = set_stool(steam_path, lua_filename, lua_content)
    log.info(f'解锁信息已保存： {out_info}')


def set_fixinfo(branch: str, steam_path: Path):
    lua_content = ''
    new_list = [(x.split('_')[0], x.split('_')[1].split('.')[0]) for x in manifests]
    for depot_id, manifest_id in new_list:
        lua_content += f'setManifestid({depot_id}, "{manifest_id}")\n'
    lua_filename = f'{branch}_F.lua'
    out_info = set_stool(steam_path, lua_filename, lua_content)
    log.info(f'清单版本已固定: {out_info}')


def manifest(repo: str, branch: str, path: str, steam_path: Path, is_ddlc=False):
    try:
        is_not_bundle = branch.isdecimal()
        url = f'https://raw.githubusercontent.com/{repo}/{branch}/{path}'
        if path.endswith('.manifest'):
            manifests.append(path)
            depot_cache = steam_path / 'depotcache'
            with lock:
                if not depot_cache.exists():
                    depot_cache.mkdir(parents=True, exist_ok=True)
            save_path = depot_cache / path
            if save_path.exists():
                with lock:
                    log.warning(f'清单已存在: {path}')
                return
            manifest_res = raw_content(url)
            with lock:
                log.info(f'清单已下载: {path}')
            with save_path.open('wb') as f:
                f.write(manifest_res)
        if path.endswith('.vdf') and path in ['config.vdf', 'Key.vdf']:
            key_res = raw_content(url)
            with lock:
                log.info(f'检测到密钥信息...')
            depot_config = vdf.loads(key_res.decode())
            depot_dict: dict = depot_config['depots']
            depot_list = [(k, v['DecryptionKey']) for k, v in depot_dict.items()]
            if is_not_bundle and not is_ddlc:
                depot_list.insert(0, (branch, None))
            log.debug(f'密钥信息: {depot_list}')
            set_appinfo(branch, depot_list, steam_path)
        if path.endswith('.json') and path in ['config.json']:
            config_res = api_request(url)
            dlcs: list[int] = config_res['dlcs'] if is_not_bundle else config_res['apps']
            ddlc: list[str] = config_res['packagedlcs'] if 'packagedlcs' in config_res else None
            depot_list = []
            appname = 'DLC' if is_not_bundle else '捆绑包'
            if not (dlcs is None) and len(dlcs) > 0:
                with lock:
                    log.info(f'检测到{appname}信息 {dlcs}...')
                depot_list.extend([(k, None) for k in dlcs])
            if not (ddlc is None) and len(ddlc) > 0:
                with lock:
                    log.info(f'检测到独立DLC信息 {ddlc}...')
                depot_list.extend([(k, None) for k in ddlc])
                for dlc in ddlc:
                    start(repo, dlc, steam_path, is_ddlc=True)
            if len(depot_list) > 0:
                log.debug(f'{appname}信息: {depot_list}')
                set_appinfo(branch, depot_list, steam_path, is_dlc=True)
    except Exception as e:
        log.error(f'出现异常: {e}')
        raise


@retry(wait_fixed=1000, stop_max_attempt_number=10)
def api_request(url: str):
    with httpx.Client() as client:
        log.debug(f'请求地址: {url}')
        headers = {'Authorization': f'Bearer {args.key}' if args.key else ''}
        result = client.get(url, headers=headers, follow_redirects=True)
        log.debug(f'结果响应: {result}')
        json: dict = result.json()
        if result.status_code == 200:
            log.debug(f'成功结果: {json}')
            return json


@retry(wait_fixed=1000, stop_max_attempt_number=10)
def raw_content(url: str):
    with httpx.Client() as client:
        log.debug(f'请求地址: {url}')
        result = client.get(url, follow_redirects=True)
        log.debug(f'结果响应: {result}')
        if result.status_code == 200:
            return result.content


def start(repo: str, branch: str, path: Path, is_ddlc=False):
    branch_res: dict = api_request(f'https://api.github.com/repos/{repo}/branches/{branch}')
    branch = branch_res['name']
    tree_url = branch_res['commit']['commit']['tree']['url']
    commit_date = branch_res['commit']['commit']['committer']['date']
    tree_res = api_request(tree_url)
    if 'tree' in tree_res:
        if branch.isdecimal() and not is_ddlc:
            set_appinfo(branch, [(branch, None)], path)
        pool_list = []
        with (Pool() as p):
            p: ThreadPool
            for i in tree_res['tree']:
                pool_list.append(p.apply_async(manifest, (repo, branch, i['path'], path, is_ddlc)))
                try:
                    while True:
                        if all([j.ready() for j in pool_list]):
                            break
                        time.sleep(0.1)
                except KeyboardInterrupt:
                    with lock:
                        p.terminate()
                    raise
            if all([j.successful() for j in pool_list]):
                if not is_ddlc:
                    if args.fixed:
                        set_fixinfo(branch, path)
                    log.info(f'清单最后更新时间: {commit_date}')
                    log.info(f'入库成功: {branch}')


def main():
    steam_path = check_steam_path()
    if not steam_path:
        log.error(f'Steam路径不存在, 可能未安装')
        return
    stool_path = check_stool_path(steam_path)
    if not stool_path:
        log.error(f'Stool路径不存在, 可能未安装')
        return
    reset_time = check_api_limit()
    if reset_time:
        log.error(f'请求次数已用尽, 重置时间: {reset_time}')
        return
    curr_repo = check_curr_repo()
    if not curr_repo:
        log.error(f'仓库暂无数据, 入库失败: {appid}')
        return
    start(curr_repo, appid, steam_path)


if __name__ == '__main__':
    version = '2.0'
    show_banner()
    args = init_args()
    log = init_logger()
    manifests: list[str] = []
    try:
        repos = init_repos()
        lock = Lock()
        time.sleep(0.1)
        init(autoreset=True)
        now = datetime.datetime.now()
        info = now.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]
        appid = args.appid or input(Fore.CYAN + f' {info} [INFO] 请输入appid: ')
        main()
    except KeyboardInterrupt:
        sys.exit()
    except Exception as err:
        log.error(f'异常错误: {err}')
    if not args.appid:
        time.sleep(0.1)
        log.critical('运行结束')
        time.sleep(0.1)
        subprocess.call('pause', shell=True)
