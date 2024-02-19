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
    print(r'''
    ________  ___  ________  ________  ________     
    |\   ____\|\  \|\   __  \|\   ____\|\   __  \    
    \ \  \___|\ \  \ \  \|\  \ \  \___|\ \  \|\  \   
     \ \  \    \ \  \ \  \\\  \ \  \    \ \  \\\  \  
      \ \  \____\ \  \ \  \\\  \ \  \____\ \  \\\  \ 
       \ \_______\ \__\ \_______\ \_______\ \_______\
        \|_______|\|__|\|_______|\|_______|\|_______|
    ''')


def init_args():
    parser = ArgumentParser()
    parser.add_argument('-v', '--version', action='version', version='%(prog)s 1.0')
    parser.add_argument('-a', '--appid', help='steam appid')
    parser.add_argument('-k', '--key', help='github API key')
    parser.add_argument('-r', '--repo', help='github repo name')
    parser.add_argument('-f', '--fixed', action='store_true', help='fixed manifest')
    parser.add_argument('-d', '--debug', action='store_true', help='debug mode')
    return parser.parse_args()


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


def init_header():
    access_token = args.key if args.key else ''
    return {'Authorization': access_token}


def init_repos():
    repo_list = ['ciocoa/manifest', 'Onekey-Project/ManifestAutoUpdate-Cache']
    if args.repo:
        repo_list.insert(0, args.repo)
    log.debug(f'仓库信息: {repo_list}')
    return repo_list


def check_steam_path():
    hkey = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam')
    steam_path = Path(winreg.QueryValueEx(hkey, 'SteamPath')[0])
    log.info(f'Steam路径: {steam_path}')
    return steam_path if 'steam.exe' in os.listdir(steam_path) else None


@retry(wait_fixed=1000, stop_max_attempt_number=10)
def check_api_limit():
    with httpx.Client() as client:
        res = client.get('https://api.github.com/rate_limit', headers=headers)
        if res.status_code == 200:
            log.debug(f'检测结果: {res.json()}')
            reset = res.json()['rate']['reset']
            remaining = res.json()['rate']['remaining']
            log.info(f'剩余请求次数: {remaining}')
            reset_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(reset))
            return reset_time if remaining == 0 else None


def check_curr_repo():
    @retry(wait_fixed=1000, stop_max_attempt_number=10)
    def get(r):
        with httpx.Client() as client:
            res = client.get(f'https://api.github.com/repos/{r}/branches/{appid}', headers=headers)
            if res.status_code == 200 and 'commit' in res.json():
                return res.json()['commit']['commit']['author']['date']

    last_date = None
    curr_repo = None
    for repo in repos:
        date = get(repo)
        if last_date is None or date > last_date:
            last_date = date
            curr_repo = repo
    log.info(f'当前清单仓库: {curr_repo}')
    return curr_repo


@retry(wait_fixed=1000, stop_max_attempt_number=10)
def raw_content(repo: str, branch: str, path: str):
    with httpx.Client() as client:
        res = client.get(f'https://raw.githubusercontent.com/{repo}/{branch}/{path}', follow_redirects=True)
        if res.status_code == 200:
            return res


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


def set_appinfo(depot_list: list, steam_path: Path, is_dlc=False, unkey=False):
    lua_content = ''
    for depot_id, depot_key in depot_list:
        lua_content += f'addappid({depot_id}, 1, "{depot_key}")\n' if depot_key else f'addappid({depot_id}, 1)\n'
    lua_filename = f'{appid}_{'D' if is_dlc else 'A'}.lua'
    out_info = set_stool(steam_path, lua_filename, lua_content)
    if not unkey:
        log.info(f'{'DLC' if is_dlc else 'APP'}脚本已保存: {out_info}')


def set_manifest(steam_path: Path):
    if args.fixed:
        log.info(f'检测到固定参数...')
        lua_content = ''
        new_list = [(x.split('_')[0], x.split('_')[1].split('.')[0]) for x in manifests]
        for depot_id, manifest_id in new_list:
            lua_content += f'setManifestid({depot_id}, "{manifest_id}")\n'
        lua_filename = f'{appid}_F.lua'
        out_info = set_stool(steam_path, lua_filename, lua_content)
        log.info(f'清单版本已固定: {out_info}')


def get_manifest(repo: str, branch: str, path: str, steam_path: Path):
    try:
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
            content = raw_content(repo, branch, path).content
            with lock:
                log.info(f'清单已下载: {path}')
            with save_path.open('wb') as f:
                f.write(content)
        if path.endswith('.vdf') and path in ['config.vdf', 'Key.vdf']:
            content = raw_content(repo, branch, path).content
            with lock:
                log.info(f'检测到密钥信息...')
            depot_config = vdf.loads(content.decode())
            depot_dict: dict = depot_config['depots']
            result = [(k, v['DecryptionKey']) for k, v in depot_dict.items()]
            result.insert(0, (branch, None))
            set_appinfo(result, steam_path)
        if path.endswith('.json') and path in ['config.json']:
            content = raw_content(repo, branch, path).json()
            dlcs: list[int] = content['dlcs']
            result = [(k, None) for k in dlcs]
            if len(result) > 0:
                with lock:
                    log.info(f'检测到DLC信息...')
                set_appinfo(result, steam_path, is_dlc=True)
    except Exception as e:
        log.error(f'出现异常: {e}')
        raise


def start():
    steam_path = check_steam_path()
    if not steam_path:
        log.error(f'Steam路径不存在, 可能未安装')
        return
    reset_time = check_api_limit()
    if reset_time:
        log.error(f'请求次数已用尽, 重置时间: {reset_time}')
        return
    curr_repo = check_curr_repo()
    if not curr_repo:
        log.error(f'仓库无数据, 入库失败: {appid}')
        return
    try:
        res = httpx.get(f'https://api.github.com/repos/{curr_repo}/branches/{appid}', headers=headers)
        if res.status_code == 200 and 'commit' in res.json():
            log.debug(f'远程信息: {res.json()}')
            branch = res.json()['name']
            tree_url = res.json()['commit']['commit']['tree']['url']
            data = res.json()['commit']['commit']['author']['date']
            r = httpx.get(tree_url, headers=headers)
            if r.status_code == 200 and 'tree' in r.json():
                log.debug(f'分支信息: {r.json()}')
                set_appinfo([(appid, None)], steam_path, unkey=True)
                pool_list = []
                with (Pool() as p):
                    p: ThreadPool
                    for i in r.json()['tree']:
                        pool_list.append(
                            p.apply_async(get_manifest, (curr_repo, branch, i['path'], steam_path))
                        )
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
                        set_manifest(steam_path)
                        log.info(f'清单最后更新时间: {data}')
                        log.info(f'入库成功: {appid}')
    except httpx.HTTPError as e:
        log.error(e)


if __name__ == '__main__':
    show_banner()
    args = init_args()
    log = init_logger()
    manifests: list[str] = []
    try:
        headers = init_header()
        repos = init_repos()
        lock = Lock()
        time.sleep(0.1)
        init(autoreset=True)
        now = datetime.datetime.now()
        info = now.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]
        appid = args.appid or input(Fore.CYAN + f' {info} [INFO] 请输入appid: ')
        start()
    except KeyboardInterrupt:
        sys.exit()
    except Exception as err:
        log.error(f'出现异常: {err}')
    if not args.appid:
        time.sleep(0.1)
        log.critical('运行结束')
        time.sleep(0.1)
        subprocess.call('pause', shell=True)
