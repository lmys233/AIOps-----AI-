"""Replace this deployment's static business targets with registered HTTP discovery."""
from pathlib import Path
import yaml

PATH=Path('deploy/prometheus/prometheus.yml')
doc=yaml.safe_load(PATH.read_text(encoding='utf-8'))
doc['scrape_configs']=[j for j in doc['scrape_configs'] if j['job_name'] not in ('aiops-services','aiops-registered-systems')]
doc['scrape_configs'].append({'job_name':'aiops-registered-systems','honor_labels':False,
    'http_sd_configs':[{'url':'http://host.docker.internal:8020/v1/observed-targets','refresh_interval':'10s'}],
    'metric_relabel_configs':[{'source_labels':['exported_'+label],'target_label':label,'action':'keepequal'}
        for label in ('observed_id','tenant','service','env')]})
PATH.write_text('# Registered SDK metrics targets; platform target discovery is not an SDK heartbeat.\n'+yaml.safe_dump(doc,sort_keys=False,allow_unicode=True),encoding='utf-8')
print('Registered HTTP discovery configured; trusted target labels must match actual SDK labels')
