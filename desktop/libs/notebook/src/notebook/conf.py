#!/usr/bin/env python
# Licensed to Cloudera, Inc. under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  Cloudera, Inc. licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json

from django.utils.translation import ugettext_lazy as _t

from desktop.lib.conf import Config, UnspecifiedConfigSection, ConfigSection


def coerce_json(j):
  return json.loads(j)


LANGUAGES = Config(
  key="languages",
  help=_t("List of available types of snippets."),
  type=coerce_json,
  default="""[
      {"name": "Scala", "type": "spark"},
      {"name": "PySpark", "type": "pyspark"},
      {"name": "R", "type": "r"},
      {"name": "Impala", "type": "impala"},
      {"name": "Hive", "type": "hive"},
      {"name": "Jar", "type": "jar"},
      {"name": "Python", "type": "py"},
      {"name": "Text", "type": "text"}
  ]"""
)

INTERPRETERS = UnspecifiedConfigSection(
  "interpreters",
  help="One entry for each type of snippet",
  each=ConfigSection(
    help="Information about a single Zookeeper cluster",
    members=dict(
      HOST_PORTS=Config(
          "name",
          help="Zookeeper ensemble. Comma separated list of Host/Port, e.g. localhost:2181,localhost:2182,localhost:2183",
          default="localhost:2181",
          type=str,
      ),
      REST_URL=Config(
          "type",
          help="The URL of the REST contrib service.",
          default="http://localhost:9998",
          type=str,
      ),
      PRINCIPAL_NAME=Config(
          "interface",
          help="Name of Kerberos principal when using security",
          default="zookeeper",
          type=str,
      ),
    )
  )
)
