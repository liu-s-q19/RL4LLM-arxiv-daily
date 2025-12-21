import os
import re
import json
import arxiv
import yaml
import logging
import argparse
import datetime
import requests

logging.basicConfig(format='[%(asctime)s %(levelname)s] %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)

github_url = "https://api.github.com/search/repositories"
arxiv_url = "http://arxiv.org/"

def load_config(config_file:str) -> dict:
    '''
    config_file: input config file path
    return: a dict of configuration
    '''
    with open(config_file,'r') as f:
        config = yaml.load(f,Loader=yaml.FullLoader)
        
        # =======================================================
        # 修改点 1: 不在这里拼接字符串，直接把列表传出去
        # =======================================================
        keywords = {}
        for k,v in config['keywords'].items():
            keywords[k] = v['filters'] # 直接传递 List
        
        config['kv'] = keywords
        logging.info(f'config = {config}')
    return config

def get_authors(authors, first_author = False):
    output = str()
    if first_author == False:
        output = ", ".join(str(author) for author in authors)
    else:
        output = authors[0]
    return output

def sort_papers(papers):
    output = dict()
    keys = list(papers.keys())
    keys.sort(reverse=True)
    for key in keys:
        output[key] = papers[key]
    return output

def get_code_link(qword:str) -> str:
    query = f"{qword}"
    params = {
        "q": query,
        "sort": "stars",
        "order": "desc"
    }
    r = requests.get(github_url, params=params)
    results = r.json()
    code_link = None
    if results["total_count"] > 0:
        code_link = results["items"][0]["html_url"]
    return code_link

def get_daily_papers(topic, query_filters, max_results=2):
    """
    @param topic: str
    @param query_filters: list of strings (keywords)
    @return paper_with_code: dict
    """
    content = dict()
    content_to_web = dict()

    # =======================================================
    # 修改点 2: 自动分块搜索 (Automatic Chunking)
    # =======================================================
    # arXiv API URL长度有限制，我们每 5 个词搜一次
    CHUNK_SIZE = 5 
    
    # 辅助函数：给多词短语加引号 (e.g. GRPO -> GRPO, RL LLM -> "RL LLM")
    def quote_filter(f):
        return f'"{f}"' if len(f.split()) > 1 else f

    # 开始循环分批搜索
    for i in range(0, len(query_filters), CHUNK_SIZE):
        # 取出一小批 (slice)
        chunk = query_filters[i : i + CHUNK_SIZE]
        
        # 拼接成 OR 查询字符串
        real_query = ' OR '.join([quote_filter(x) for x in chunk])
        
        logging.info(f"🔍 Searching Chunk {i//CHUNK_SIZE + 1}: {real_query}")

        try:
            search_engine = arxiv.Search(
                query = real_query,
                max_results = max_results,
                sort_by = arxiv.SortCriterion.SubmittedDate
            )

            for result in search_engine.results():
                
                paper_id            = result.get_short_id()
                paper_title         = result.title
                paper_url           = result.entry_id
                paper_abstract      = result.summary.replace("\n"," ")
                paper_authors       = get_authors(result.authors)
                paper_first_author  = get_authors(result.authors,first_author = True)
                update_time         = result.updated.date()

                # 处理版本号
                ver_pos = paper_id.find('v')
                if ver_pos == -1:
                    paper_key = paper_id
                else:
                    paper_key = paper_id[0:ver_pos]
                
                paper_url = arxiv_url + 'abs/' + paper_key

                # 存入字典 (字典天然去重：如果不同 Chunk 搜到了同一篇论文，会直接覆盖，不影响结果)
                content[paper_key] = "|**{}**|**{}**|{} et.al.|[{}]({})|null|\n".format(
                       update_time,paper_title,paper_first_author,paper_key,paper_url)
                
                content_to_web[paper_key] = "- {}, **{}**, {} et.al., Paper: [{}]({})".format(
                       update_time,paper_title,paper_first_author,paper_url,paper_url)

        except Exception as e:
            logging.error(f"⚠️ Error searching chunk {real_query}: {str(e)}")
            continue # 如果这一批报错，继续搜下一批

    return {topic: content}, {topic: content_to_web}

def update_paper_links(filename):
    def parse_arxiv_string(s):
        parts = s.split("|")
        date = parts[1].strip()
        title = parts[2].strip()
        authors = parts[3].strip()
        arxiv_id = parts[4].strip()
        code = parts[5].strip()
        arxiv_id = re.sub(r'v\d+', '', arxiv_id)
        return date,title,authors,arxiv_id,code

    with open(filename,"r") as f:
        content = f.read()
        if not content:
            m = {}
        else:
            m = json.loads(content)

        json_data = m.copy()

        for keywords,v in json_data.items():
            for paper_id,contents in v.items():
                contents = str(contents)
                update_time, paper_title, paper_first_author, paper_url, code_url = parse_arxiv_string(contents)
                contents = "|{}|{}|{}|{}|{}|\n".format(update_time,paper_title,paper_first_author,paper_url,code_url)
                json_data[keywords][paper_id] = str(contents)
        
        with open(filename,"w") as f:
            json.dump(json_data,f)

def update_json_file(filename,data_dict):
    with open(filename,"r") as f:
        content = f.read()
        if not content:
            m = {}
        else:
            m = json.loads(content) # 如果想清空历史，把这行改成 m = {}

    json_data = m.copy()

    for data in data_dict:
        for keyword in data.keys():
            papers = data[keyword]
            if keyword in json_data.keys():
                json_data[keyword].update(papers)
            else:
                json_data[keyword] = papers

    with open(filename,"w") as f:
        json.dump(json_data,f)

def json_to_md(filename, md_filename, 
               user_name, repo_name,
               task = '',
               to_web = False,
               use_title = True,
               use_tc = True,
               show_badge = True,
               use_b2t = True):
    
    def pretty_math(s:str) -> str:
        ret = ''
        match = re.search(r"\$.*\$", s)
        if match == None:
            return s
        math_start,math_end = match.span()
        space_trail = space_leading = ''
        if s[:math_start][-1] != ' ' and '*' != s[:math_start][-1]: space_trail = ' '
        if s[math_end:][0] != ' ' and '*' != s[math_end:][0]: space_leading = ' '
        ret += s[:math_start]
        ret += f'{space_trail}${match.group()[1:-1].strip()}${space_leading}'
        ret += s[math_end:]
        return ret

    DateNow = datetime.date.today()
    DateNow = str(DateNow)
    DateNow = DateNow.replace('-','.')

    with open(filename,"r") as f:
        content = f.read()
        if not content:
            data = {}
        else:
            data = json.loads(content)

    with open(md_filename,"w+") as f:
        pass

    with open(md_filename,"a+") as f:
        if (use_title == True) and (to_web == True):
            f.write("---\n" + "layout: default\n" + "---\n\n")

        if use_title == True:
            f.write("## Updated on " + DateNow + "\n")
        else:
            f.write("> Updated on " + DateNow + "\n")

        f.write("> Usage instructions: [here](./docs/README.md#usage)\n\n")

        if use_tc == True:
            f.write("<details>\n")
            f.write("  <summary>Table of Contents</summary>\n")
            f.write("  <ol>\n")
            for keyword in data.keys():
                day_content = data[keyword]
                if not day_content:
                    continue
                kw = keyword.replace(' ','-')
                f.write(f"    <li><a href=#{kw.lower()}>{keyword}</a></li>\n")
            f.write("  </ol>\n")
            f.write("</details>\n\n")

        for keyword in data.keys():
            day_content = data[keyword]
            if not day_content:
                continue
            f.write(f"## {keyword}\n\n")

            if use_title == True :
                if to_web == False:
                    f.write("|Publish Date|Title|Authors|PDF|Code|\n" + "|---|---|---|---|---|\n")
                else:
                    f.write("| Publish Date | Title | Authors | PDF | Code |\n")
                    f.write("|:---------|:-----------------------|:---------|:------|:------|\n")

            day_content = sort_papers(day_content)

            for _,v in day_content.items():
                if v is not None:
                    f.write(pretty_math(v))

            f.write(f"\n")

            if use_b2t:
                top_info = f"#Updated on {DateNow}"
                top_info = top_info.replace(' ','-').replace('.','')
                f.write(f"<p align=right>(<a href={top_info.lower()}>back to top</a>)</p>\n\n")

        if show_badge == True:
            f.write((f"[contributors-shield]: https://img.shields.io/github/"
                     f"contributors/{user_name}/{repo_name}.svg?style=for-the-badge\n"))
            f.write((f"[contributors-url]: https://github.com/{user_name}/"
                     f"{repo_name}/graphs/contributors\n"))
            f.write((f"[forks-shield]: https://img.shields.io/github/forks/{user_name}/"
                     f"{repo_name}.svg?style=for-the-badge\n"))
            f.write((f"[forks-url]: https://github.com/{user_name}/"
                     f"{repo_name}/network/members\n"))
            f.write((f"[stars-shield]: https://img.shields.io/github/stars/{user_name}/"
                     f"{repo_name}.svg?style=for-the-badge\n"))
            f.write((f"[stars-url]: https://github.com/{user_name}/"
                     f"{repo_name}/stargazers\n"))
            f.write((f"[issues-shield]: https://img.shields.io/github/issues/{user_name}/"
                     f"{repo_name}.svg?style=for-the-badge\n"))
            f.write((f"[issues-url]: https://github.com/{user_name}/"
                     f"{repo_name}/issues\n\n"))

    logging.info(f"{task} finished")

def demo(**config):
    data_collector = []
    data_collector_web= []

    keywords = config['kv']
    max_results = config['max_results']
    publish_readme = config['publish_readme']
    publish_gitpage = config['publish_gitpage']
    publish_wechat = config['publish_wechat']
    show_badge = config['show_badge']
    
    user_name = config.get('user_name', 'liu-s-q19')
    repo_name = config.get('repo_name', 'RL4LLM-arxiv-daily')

    b_update = config['update_paper_links']
    logging.info(f'Update Paper Link = {b_update}')
    
    if config['update_paper_links'] == False:
        logging.info(f"GET daily papers begin")
        for topic, query_filters in keywords.items():
            logging.info(f"Keyword Topic: {topic}")
            # 注意：这里传进去的 query_filters 现在是一个 List
            data, data_web = get_daily_papers(topic, query_filters = query_filters,
                                              max_results = max_results)
            data_collector.append(data)
            data_collector_web.append(data_web)
            print("\n")
        logging.info(f"GET daily papers end")

    if publish_readme:
        json_file = config['json_readme_path']
        md_file   = config['md_readme_path']
        if config['update_paper_links']:
            update_paper_links(json_file)
        else:
            update_json_file(json_file,data_collector)
        json_to_md(json_file, md_file, user_name, repo_name, 
                   task ='Update Readme', show_badge = show_badge)

    if publish_gitpage:
        json_file = config['json_gitpage_path']
        md_file   = config['md_gitpage_path']
        if config['update_paper_links']:
            update_paper_links(json_file)
        else:
            update_json_file(json_file,data_collector)
        json_to_md(json_file, md_file, user_name, repo_name, 
                   task ='Update GitPage', to_web = True, show_badge = show_badge, 
                   use_tc=False, use_b2t=False)

    if publish_wechat:
        json_file = config['json_wechat_path']
        md_file   = config['md_wechat_path']
        if config['update_paper_links']:
            update_paper_links(json_file)
        else:
            update_json_file(json_file, data_collector_web)
        json_to_md(json_file, md_file, user_name, repo_name, 
                   task ='Update Wechat', to_web=False, use_title= False, show_badge = show_badge)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--config_path',type=str, default='config.yaml',
                            help='configuration file path')
    parser.add_argument('--update_paper_links', default=False,
                        action="store_true",help='whether to update paper links etc.')
    args = parser.parse_args()
    config = load_config(args.config_path)
    config = {**config, 'update_paper_links':args.update_paper_links}
    demo(**config)
