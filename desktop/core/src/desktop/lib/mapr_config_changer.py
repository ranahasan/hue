import re
import os

MAPR_SECURITY = "MAPR-SECURITY"
SECURE = "secure"
SECURITY_ENABLED = 'security_enabled'
MECHANISM = 'mechanism'

MAPR_CLUSTERS_CONF_PATH = "/opt/mapr/conf/mapr-clusters.conf"

templates = {
  MECHANISM: 'none',
  SECURITY_ENABLED: 'false'
}

def read_values_from_mapr_clusters_conf():
  if not os.path.exists(MAPR_CLUSTERS_CONF_PATH):
    return

  mapr_clusters_conf = open(MAPR_CLUSTERS_CONF_PATH, "r").read()
  cluster_props = dict(re.findall(r'(\S+)=(".*?"|\S+)', mapr_clusters_conf))

  templates[SECURITY_ENABLED] = cluster_props[SECURE] if SECURE in cluster_props else "false"

  if templates[SECURITY_ENABLED] == "true":
    templates[MECHANISM] = MAPR_SECURITY


templateRegEx = re.compile(r'^\${(.+?)}')

def change_config(config):
  for key in config:
    if isinstance(config[key], dict):
      change_config(config[key])
    elif type(config[key]) == str:
      match = templateRegEx.search(config[key])
      if (match != None) and (match.group(1) in templates):
        config[key] = templates[match.group(1)]

  return config

def fill_templates(config):
  read_values_from_mapr_clusters_conf()
  change_config(config)

  return config

