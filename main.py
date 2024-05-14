import logging
import os
import subprocess
import sys
import time
import winreg
from argparse import ArgumentParser
from datetime import datetime
from multiprocessing import Lock, pool
from pathlib import Path

import httpx
import vdf
from colorama import Fore
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


def init_args():
    parser = ArgumentParser()
    parser.add_argument('-v', '--version', action='version', version=f'%(prog)s v{version}')
    parser.add_argument('-a', '--appid', help='steam appid')
    parser.add_argument('-k', '--key', help='github API key')
    parser.add_argument('-r', '--repo', help='github repo name')
    parser.add_argument('-f', '--fixed', action='store_true', help='fixed manifest')
    parser.add_argument('-d', '--debug', action='store_true', help='debug mode')
    return parser.parse_args()


class MainApp:
    def __init__(self):
        self.args = init_args()
        self.logr = self.init_logger()
        self.repos = self.init_repos()
        self.manifests: list[str] = []
        self.depots: list[tuple[int, str | None]] = []
        self.lock = Lock()
        self.appid = self.get_appid()

    def init_logger(self):
        logger = logging.getLogger(__name__)
        handler = logging.StreamHandler()
        formatter = ColoredFormatter('%(log_color)s %(asctime)s [%(levelname)s] %(message)s')
        handler.setFormatter(formatter)
        level = logging.DEBUG if self.args.debug else logging.INFO
        logger.addHandler(handler)
        logger.setLevel(level)
        return logger

    def init_repos(self):
        repo_list = ['ciocoa/manifest']
        repo = self.args.repo
        if repo:
            repo_list.insert(0, repo)
        return repo_list

    def get_appid(self):
        input_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]
        prompt = f'{Fore.CYAN} {input_time} [INFO] 请输入appid: '
        try:
            appid = self.args.appid or input(prompt)
            return appid
        except KeyboardInterrupt:
            sys.exit()

    def run(self):
        try:
            self.main()
        except KeyboardInterrupt:
            sys.exit()
        except Exception as e:
            self.logr.error(f'异常错误: {e}')
        if not self.args.appid:
            self.logr.critical('运行结束')
            time.sleep(0.1)
            subprocess.call('pause', shell=True)

    def main(self):
        steam_path = self.check_steam_path()
        if not steam_path:
            self.logr.error(f'Steam路径不存在')
            return
        lua_path = self.check_lua_path(steam_path)
        if not lua_path:
            self.logr.error(f'Luapacka路径不存在')
            return
        reset_time = self.check_api_limit()
        if reset_time:
            self.logr.error(f'请求次数已用尽, 重置时间: {reset_time}')
            return
        curr_repo = self.check_curr_repo()
        if not curr_repo:
            self.logr.error(f'仓库暂无数据, 入库失败: {self.appid}')
            return
        self.start(curr_repo, self.appid, steam_path)

    def check_steam_path(self):
        try:
            hkey = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam')
            steam_path = Path(winreg.QueryValueEx(hkey, 'SteamPath')[0])
            if 'steam.exe' in os.listdir(steam_path):
                self.logr.info(f'检测到Steam: {steam_path}')
                return steam_path
        except Exception as e:
            self.logr.error(e)

    def check_lua_path(self, path: Path):
        try:
            lua_path = path / 'config' / 'stplug-in'
            if 'luapacka.exe' in os.listdir(lua_path):
                self.logr.info(f'检测到Luapacka: {lua_path}')
                return lua_path
        except Exception as e:
            self.logr.error(e)

    def check_api_limit(self):
        limit_res = self.api_request('https://api.github.com/rate_limit')
        reset = limit_res['rate']['reset']
        remaining = limit_res['rate']['remaining']
        self.logr.info(f'剩余请求次数: {remaining}')
        reset_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(reset))
        if remaining == 0:
            return reset_time

    def check_curr_repo(self):
        last_date = None
        curr_repo = None
        for repo in self.repos:
            branch_res = self.api_request(f'https://api.github.com/repos/{repo}/branches/{self.appid}')
            if branch_res and 'commit' in branch_res:
                date = branch_res['commit']['commit']['committer']['date']
                if last_date is None or date > last_date:
                    last_date = date
                    curr_repo = repo
        self.logr.info(f'当前清单仓库: {curr_repo}')
        return curr_repo

    def start(self, repo: str, branch: str, path: Path, is_dlc=False):
        branch_res = self.api_request(f'https://api.github.com/repos/{repo}/branches/{branch}')
        if not branch_res or 'commit' not in branch_res:
            return
        tree_url = branch_res['commit']['commit']['tree']['url']
        commit_date = branch_res['commit']['commit']['committer']['date']
        tree_res = self.api_request(tree_url)
        if not tree_res or 'tree' not in tree_res:
            return
        self.depots.append((int(branch), None))
        with pool.ThreadPool() as tpool:
            tasks = [tpool.apply_async(self.manifest, (repo, branch, tree['path'], path)) for tree in tree_res['tree']]
            try:
                for task in tasks:
                    task.get()
            except KeyboardInterrupt:
                with self.lock:
                    tpool.terminate()
                raise
        if all(task.successful() for task in tasks) and not is_dlc:
            self.set_appinfo(path)
            self.logr.info(f'清单最后更新时间: {commit_date}')
            self.logr.info(f'入库成功: {self.appid}')

    def manifest(self, repo: str, branch: str, path: str, steam_path: Path):
        try:
            url = f'https://raw.githubusercontent.com/{repo}/{branch}/{path}'
            if path.endswith('.manifest'):
                self.manifests.append(path)
                depot_cache = steam_path / 'depotcache'
                with self.lock:
                    if not depot_cache.exists():
                        depot_cache.mkdir(parents=True, exist_ok=True)
                save_path = depot_cache / path
                if save_path.exists():
                    with self.lock:
                        self.logr.warning(f'清单已存在: {path}')
                    return
                manifest_res = self.raw_content(url)
                with self.lock:
                    self.logr.info(f'清单已下载: {path}')
                with save_path.open('wb') as f:
                    f.write(manifest_res)
            if path.endswith('.vdf') and path in ['config.vdf']:
                key_res = self.raw_content(url)
                with self.lock:
                    self.logr.info(f'检测到密钥信息...')
                depot_config = vdf.loads(key_res.decode())
                depot_dict: dict = depot_config['depots']
                self.depots.extend((int(k), v['DecryptionKey']) for k, v in depot_dict.items())
                self.logr.debug(f'密钥信息: {depot_dict}')
            if path.endswith('.json') and path in ['config.json']:
                config_res = self.api_request(url)
                dlcs: list[int | str] = config_res['dlcs']
                ddlc: list[int | str] = config_res['packagedlcs']
                if dlcs and len(dlcs) > 0:
                    with self.lock:
                        self.logr.info(f'检测到DLC信息 {dlcs}...')
                    self.depots.extend((k, None) for k in dlcs)
                if ddlc and len(ddlc) > 0:
                    with self.lock:
                        self.logr.info(f'检测到独立DLC {ddlc}...')
                    [self.start(repo, dlc, steam_path, True) for dlc in ddlc]
        except Exception as e:
            self.logr.error(f'出现异常: {e}')
            raise

    def set_appinfo(self, path: Path):
        depot_list = sorted(set(self.depots), key=lambda x: x[0])
        self.logr.debug(depot_list)
        lua_content = ''.join(
            f'addappid({depot_id}, 1, "{depot_key}")\n' if depot_key else f'addappid({depot_id}, 1)\n' for
            depot_id, depot_key in depot_list)
        if self.args.fixed:
            manifest_list = sorted(
                [(split_x[0], split_x[1].split('.')[0]) for split_x in (x.split('_') for x in self.manifests)],
                key=lambda x: x[0])
            lua_content += ''.join(
                f'setManifestid({depot_id}, "{manifest_id}")\n' for depot_id, manifest_id in manifest_list)
            self.logr.debug(manifest_list)
        lua_filepath = path / 'config' / 'stplug-in' / f'{self.appid}.lua'
        with open(lua_filepath, 'w') as f:
            f.write(lua_content)
        lua_packpath = path / 'config' / 'stplug-in' / 'luapacka.exe'
        result = subprocess.run([str(lua_packpath), str(lua_filepath)], stdout=subprocess.PIPE)
        if not self.args.debug:
            os.remove(lua_filepath)
        output = result.stdout.decode('utf-8').removesuffix('\r\n')
        self.logr.info(f'解锁信息已保存： {output}')

    @retry(wait_fixed=1000, stop_max_attempt_number=10)
    def api_request(self, url: str):
        with httpx.Client() as client:
            self.logr.debug(f'请求地址: {url}')
            token = os.getenv('GITHUB_API_TOKEN') or self.args.key
            headers = {'Authorization': f'Bearer {token}' if token else ''}
            result = client.get(url, headers=headers, follow_redirects=True)
            self.logr.debug(f'结果响应: {result}')
            json: dict = result.json()
            if result.status_code == 200:
                self.logr.debug(f'成功结果: {json}')
                return json

    @retry(wait_fixed=1000, stop_max_attempt_number=10)
    def raw_content(self, url: str):
        with httpx.Client() as client:
            self.logr.debug(f'请求地址: {url}')
            result = client.get(url, follow_redirects=True)
            self.logr.debug(f'结果响应: {result}')
            if result.status_code == 200:
                return result.content


if __name__ == '__main__':
    version = '3.0'
    show_banner()
    MainApp().run()
