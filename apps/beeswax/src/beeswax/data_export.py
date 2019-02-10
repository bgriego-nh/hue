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
import logging
import math
import types
import datetime

from django.utils.translation import ugettext as _

from desktop.lib import export_csvxls
from beeswax import common, conf


LOG = logging.getLogger(__name__)


FETCH_SIZE = 10000
DOWNLOAD_COOKIE_AGE = 1800 # 30 minutes


def download(handle, format, db, id=None, file_name='query_result', user_agent=None, callback=None):
  """
  download(query_model, format) -> HttpResponse

  Retrieve the query result in the format specified. Return an HttpResponse object.
  """
  if format not in common.DL_FORMATS:
    LOG.error('Unknown download format "%s"' % (format,))
    return

  max_rows = conf.DOWNLOAD_ROW_LIMIT.get()
  max_bytes = conf.DOWNLOAD_BYTES_LIMIT.get()

  content_generator = HS2DataAdapter(handle, db, max_rows=max_rows, start_over=True, max_bytes=max_bytes, callback=callback)
  generator = export_csvxls.create_generator(content_generator, format)

  resp = export_csvxls.make_response(generator, format, file_name, user_agent=user_agent)

  if id:
    resp.set_cookie(
      'download-%s' % id,
      json.dumps({
        'truncated': content_generator.is_truncated,
        'row_counter': content_generator.row_counter
      }),
      max_age=DOWNLOAD_COOKIE_AGE
    )

  return resp


def upload(path, handle, user, db, fs, max_rows=-1, max_bytes=-1, source=None):
  """
  upload(query_model, path, user, db, fs) -> None

  Retrieve the query result in the format specified and upload to hdfs.
  """
  if fs.do_as_user(user.username, fs.exists, path):
    raise Exception(_("%s already exists.") % path)
  else:
    fs.do_as_user(user.username, fs.create, path)

  content_generator = HS2DataAdapter(handle, db, max_rows=max_rows, start_over=True, max_bytes=max_bytes, source=source)
  for header, data in content_generator:
    dataset = export_csvxls.dataset(None, data)
    fs.do_as_user(user.username, fs.append, path, dataset.csv)


class HS2DataAdapter:

  def __init__(self, handle, db, max_rows=-1, start_over=True, max_bytes=-1, callback=None, source=None):
    self.handle = handle
    self.db = db
    self.max_rows = max_rows
    self.max_bytes = max_bytes
    self.start_over = start_over
    self.fetch_size = FETCH_SIZE
    self.limit_rows = max_rows > -1
    self.limit_bytes = max_bytes > -1
    self.callback = callback
    self.source = source

    self.first_fetched = True
    self.headers = None
    self.num_cols = None
    self.row_counter = 1
    self.bytes_counter = 0
    self.is_truncated = False
    self.has_more = True
    self._results = None
    if self.source == 'rdbms':
      self._results = self.db.execute_statement(self.handle, fetch_max=max_rows)

  def __iter__(self):
    return self

  # Return an estimate of the size of the object using only ascii characters once serialized to string.
  # Avoid serialization to string where possible
  def _getsizeofascii(self, row):
    size = 0
    size += max(len(row) - 1, 0) # CSV commas between columns
    size += 2 # CSV \r\n at the end of row
    for col in row:
      col_type = type(col)
      if col_type == types.IntType:
        if col == 0:
          size += 1
        elif col < 0:
          size += int(math.log10(-1 * col)) + 2
        else:
          size += int(math.log10(col)) + 1
      elif col_type == types.StringType:
        size += len(col)
      elif col_type == types.FloatType:
        size += len(str(col))
      elif col_type == types.BooleanType:
        size += 4
      elif col_type == types.NoneType:
        size += 4
      else:
        size += len(str(col))

    return size

  def next(self):
    if self.source=='rdbms':
      results = self._results
    else:
      results = self.db.fetch(self.handle, start_over=self.start_over, rows=self.fetch_size)

    if self.first_fetched:
      self.first_fetched = False
      self.start_over = False
      self.headers = results.cols()
      self.num_cols = len(self.headers)
      if self.limit_bytes:
        self.bytes_counter += max(self.num_cols - 1, 0)
        for header in self.headers:
          self.bytes_counter += len(header)

      # For result sets with high num of columns, fetch in smaller batches to avoid serialization cost
      if self.num_cols > 100:
         self.fetch_size = 100

    if self.has_more and not self.is_truncated:
      self.has_more = results.has_more
      data = []

      rdbms_fetch_cnt = 0
      for row in results.rows():
        self.row_counter += 1
        if self.limit_bytes:
          self.bytes_counter += self._getsizeofascii(row)

        if self.limit_rows and self.row_counter > self.max_rows:
          LOG.warn('The query results exceeded the maximum row limit of %d and has been truncated to first %d rows.' % (self.max_rows, self.row_counter))
          self.is_truncated = True
          break
        if self.limit_bytes and self.bytes_counter > self.max_bytes:
          LOG.warn('The query results exceeded the maximum bytes limit of %d and has been truncated to first %d rows.' % (self.max_bytes, self.row_counter))
          self.is_truncated = True
          break
        data.append(row)

        # add break for rdbms sources to prevent fetch all
        rdbms_fetch_cnt += 1
        if self.source == 'rdbms' and rdbms_fetch_cnt >= self.fetch_size:
          break

      return self.headers, data
    else:
      if self.callback:
        self.callback()
      raise StopIteration
