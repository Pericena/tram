import json
import asyncio
import queue
from multiprocessing import Process, Pool, Pipe, cpu_count
from time import sleep

class RestService:

    def __init__(self, web_svc, reg_svc, data_svc, ml_svc, dao):
        self.dao = dao
        self.data_svc = data_svc
        self.web_svc = web_svc
        self.ml_svc = ml_svc
        self.reg_svc = reg_svc
        self.resources = []
        self.monitor,child_conn = Pipe() # create pipe for server to comm with manager process
        self.process_manager = Process(target=self.check_queue,args=(child_conn,)).start() # create manager process

    async def false_negative(self, criteria=None):
        sentence_dict = await self.dao.get('report_sentences', dict(uid=criteria['sentence_id']))
        sentence_to_strip = sentence_dict[0]['text']
        sentence_to_insert = self.web_svc.remove_html_markup_and_found(sentence_to_strip)
        await self.dao.insert('false_negatives', dict(sentence_id=sentence_dict[0]['uid'], uid=criteria['attack_uid'],
                                                      false_negative=sentence_to_insert))
        return dict(status='inserted')

    async def set_status(self, criteria=None):
        report_dict = await self.dao.get('reports', dict(title=criteria['file_name']))
        await self.dao.update('reports', 'uid', report_dict[0]['uid'], dict(current_status=criteria['set_status']))
        return dict(status="Report status updated to " + criteria['set_status'])

    async def delete_report(self, criteria=None):
        await self.dao.delete('reports', dict(uid=criteria['report_id']))
        await self.dao.delete('report_sentences', dict(report_uid=criteria['report_id']))
        await self.dao.delete('report_sentence_hits', dict(report_uid=criteria['report_id']))

    async def remove_sentences(self, criteria=None):
        if not criteria['sentence_id']:
            return dict(status="Please enter a number.")
        else:
            true_positives = await self.dao.get('true_positives', dict(sentence_id=criteria['sentence_id']))
            false_positives = await self.dao.get('false_positives', dict(sentence_id=criteria['sentence_id']))
            false_negatives = await self.dao.get('false_negatives', dict(sentence_id=criteria['sentence_id']))
        if not true_positives and not false_positives and not false_negatives:
            return dict(status="There is no entry for sentence id " + criteria['sentence_id'])
        else:
            await self.dao.delete('true_positives', dict(sentence_id=criteria['sentence_id']))
            await self.dao.delete('false_positives', dict(sentence_id=criteria['sentence_id']))
            await self.dao.delete('false_negatives', dict(sentence_id=criteria['sentence_id']))
            return dict(status='Successfully moved sentence ' + criteria['sentence_id'])

    async def sentence_context(self, criteria=None):
        sentence_hits = await self.dao.get('report_sentence_hits', dict(uid=criteria['uid']))
        return sentence_hits

    async def confirmed_sentences(self, criteria=None):
        tmp = []
        techniques = await self.dao.get('true_positives', dict(sentence_id=criteria['sentence_id']))
        for tech in techniques:
            name = await self.dao.get('attack_uids', dict(uid=tech['uid']))
            tmp.append(name[0])
        return tmp

    async def true_positive(self, criteria=None):
        sentence_dict = await self.dao.get('report_sentences', dict(uid=criteria['sentence_id']))
        sentence_to_insert = await self.web_svc.remove_html_markup_and_found(sentence_dict[0]['text'])
        await self.dao.insert('true_positives', dict(sentence_id=sentence_dict[0]['uid'], uid=criteria['attack_uid'],
                                                     true_positive=sentence_to_insert))
        return dict(status='inserted')

    async def false_positive(self, criteria=None):
        sentence_dict = await self.dao.get('report_sentences', dict(uid=criteria['sentence_id']))
        sentence_to_insert = await self.web_svc.remove_html_markup_and_found(sentence_dict[0]['text'])
        last = await self.data_svc.last_technique_check(criteria)
        await self.dao.insert('false_positives', dict(sentence_id=sentence_dict[0]['uid'], uid=criteria['attack_uid'],
                                                      false_positive=sentence_to_insert))
        return dict(status='inserted', last=last)

    async def insert_report(self, criteria=None):
        #criteria['id'] = await self.dao.insert('reports', dict(title=criteria['title'], url=criteria['url'],
        #                                                       current_status="needs_review"))
        criteria = dict(title=criteria['title'], url=criteria['url'],current_status="needs_review")
        asyncio.create_task(self.check_queue(criteria)) # self.monitor.send(criteria) # send needed data to monitor process
        await asyncio.sleep(0.01)
    

    async def check_queue(self,conn):
        '''
        description: runs as a child process that spawns worker processes via the multiprocessing library
        acts as manager for concurrent report analysis
        input: pipe connection
        output: nil
        '''
        resources = [] # currently running processes
        man_queue = asyncio.Queue() # manager queue for work to be done
        max_workers = 1#cpu_count() # num workers based on num cpus
        while(True):
            
            print(conn)
            await man_queue.put(conn) # get data from pipe
            print(man_queue)
            while(not man_queue.empty()):
                for proc in range(len(resources)):
                    if(not resources[proc].is_alive()):
                        del resources[proc] # if the process is finished, or dead, remove it from resources
                if(len(resources) >= max_workers):
                    await asyncio.sleep(1) # wait if resources are maxed
                    print("Processing data, current processing workers: {}".format(resources))
                else:
                    to_process = await man_queue.get() # get next thing to do off queue
                    resources.append(to_process)
                    await self.start_analysis(to_process)
                    #p = Process(target=self.analysis_wrapper,args=(to_process,)) # and analyze it
                    #resources.append(p)
                    #p.start()
            print("BEFORE!!!!")
            await asyncio.sleep(0.0001)
            print("AFTER!!!!")
            

    def analysis_wrapper(self,criteria):
        '''
        description: Acts as child process to run event loop for analysis
        input: criteria
        output: nil
        '''
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.start_analysis(criteria))

    async def start_analysis(self, criteria=None):
        tech_data = await self.dao.get('attack_uids')
        json_tech = json.load(open("models/attack_dict.json", "r", encoding="utf_8"))
        techniques = {}
        for row in tech_data:
            await asyncio.sleep(0.01)
            # skip software
            if 'tool' in row['tid'] or 'malware' in row['tid']:
                continue
            else:
                # query for true positives
                true_pos = await self.dao.get('true_positives', dict(uid=row['uid']))
                tp, fp = [], []
                for t in true_pos:
                    tp.append(t['true_positive'])
                # query for false negatives
                false_neg = await self.dao.get('false_negatives', dict(uid=row['uid']))
                for f in false_neg:
                    tp.append(f['false_negative'])
                # query for false positives for this technique
                false_positives = await self.dao.get('false_positives', dict(uid=row['uid']))
                for fps in false_positives:
                    fp.append(fps['false_positive'])

                techniques[row['uid']] = {'id': row['tid'], 'name': row['name'], 'similar_words': [],
                                          'example_uses': tp, 'false_positives': fp}

        html_data = await self.web_svc.get_url(criteria['url'])
        original_html = await self.web_svc.map_all_html(criteria['url'])

        article = dict(title=criteria['title'], html_text=html_data)
        list_of_legacy, list_of_techs = await self.data_svc.ml_reg_split(json_tech)

        true_negatives = await self.ml_svc.get_true_negs()
        # Here we build the sentence dictionary
        html_sentences = await self.web_svc.tokenize_sentence(article['html_text'])
        model_dict = await self.ml_svc.build_pickle_file(list_of_techs, json_tech, true_negatives)

        ml_analyzed_html = await self.ml_svc.analyze_html(list_of_techs, model_dict, html_sentences)
        regex_patterns = await self.dao.get('regex_patterns')
        reg_analyzed_html = self.reg_svc.analyze_html(regex_patterns, html_sentences)

        # Merge ML and Reg hits
        analyzed_html = await self.ml_svc.combine_ml_reg(ml_analyzed_html, reg_analyzed_html)

        criteria['id'] = await self.dao.insert('reports', dict(title=criteria['title'], url=criteria['url'],
                                                                            current_status="needs_review"))
        report_id = criteria['id']
        for sentence in analyzed_html:
            if sentence['ml_techniques_found']:
                await self.ml_svc.ml_techniques_found(report_id, sentence)
            elif sentence['reg_techniques_found']:
                await self.reg_svc.reg_techniques_found(report_id, sentence)
            else:
                data = dict(report_uid=report_id, text=sentence['text'], html=sentence['html'], found_status="false")
                await self.dao.insert('report_sentences', data)

        for element in original_html:
            html_element = dict(report_uid=report_id, text=element['text'], tag=element['tag'], found_status="false")
            await self.dao.insert('original_html', html_element)

    async def missing_technique(self, criteria=None):
        attack_uid = await self.dao.get('attack_uids', dict(tid=criteria['tid']))
        criteria['attack_uid'] = attack_uid[0]['uid']
        await self.true_positive(criteria)
        return attack_uid[0]['uid']

