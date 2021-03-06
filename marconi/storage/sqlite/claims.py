# Copyright (c) 2013 Rackspace, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from marconi.storage import base
from marconi.storage import exceptions
from marconi.storage.sqlite import utils


class ClaimController(base.ClaimBase):
    def __init__(self, driver):
        self.driver = driver
        self.driver.run('''
            create table
            if not exists
            Claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                qid INTEGER,
                ttl INTEGER,
                created DATETIME,  -- seconds since the Julian day
                FOREIGN KEY(qid) references Queues(id) on delete cascade
            )
        ''')
        self.driver.run('''
            create table
            if not exists
            Locked (
                cid INTEGER,
                msgid INTEGER,
                FOREIGN KEY(cid) references Claims(id) on delete cascade,
                FOREIGN KEY(msgid) references Messages(id) on delete cascade
            )
        ''')

    def get(self, queue, claim_id, project):
        if project is None:
            project = ''

        with self.driver('deferred'):
            try:
                id, ttl, age = self.driver.get('''
                    select C.id, C.ttl, julianday() * 86400.0 - C.created
                      from Queues as Q join Claims as C
                        on Q.id = C.qid
                     where C.ttl > julianday() * 86400.0 - C.created
                       and C.id = ? and project = ? and name = ?
                ''', utils.cid_decode(claim_id), project, queue)

                return (
                    {
                        'id': claim_id,
                        'ttl': ttl,
                        'age': int(age),
                    },
                    self.__get(id)
                )

            except (utils.NoResult, exceptions.MalformedID()):
                raise exceptions.ClaimDoesNotExist(claim_id, queue, project)

    def create(self, queue, metadata, project, limit=10):
        if project is None:
            project = ''

        with self.driver('immediate'):
            qid = utils.get_qid(self.driver, queue, project)

            # Clean up all expired claims in this queue

            self.driver.run('''
                delete from Claims
                 where ttl <= julianday() * 86400.0 - created
                   and qid = ?''', qid)

            self.driver.run('''
                insert into Claims
                values (null, ?, ?, julianday() * 86400.0)
            ''', qid, metadata['ttl'])

            id = self.driver.lastrowid

            self.driver.run('''
                insert into Locked
                select last_insert_rowid(), id
                  from Messages left join Locked
                    on id = msgid
                 where msgid is null
                   and ttl > julianday() * 86400.0 - created
                   and qid = ?
                 limit ?''', qid, limit)

            messages_ttl = metadata['ttl'] + metadata['grace']
            self.__update_claimed(id, messages_ttl)

            return (utils.cid_encode(id), self.__get(id))

    def __get(self, cid):
        records = self.driver.run('''
            select id, content, ttl, julianday() * 86400.0 - created
              from Messages join Locked
                on msgid = id
             where ttl > julianday() * 86400.0 - created
               and cid = ?''', cid)

        for id, content, ttl, age in records:
            yield {
                'id': utils.msgid_encode(id),
                'ttl': ttl,
                'age': int(age),
                'body': content,
            }

    def update(self, queue, claim_id, metadata, project):
        if project is None:
            project = ''

        try:
            id = utils.cid_decode(claim_id)
        except exceptions.MalformedID:
            raise exceptions.ClaimDoesNotExist(claim_id, queue, project)

        with self.driver('deferred'):

            # still delay the cleanup here
            self.driver.run('''
                update Claims
                   set created = julianday() * 86400.0,
                       ttl = ?
                 where ttl > julianday() * 86400.0 - created
                   and id = ?
                   and qid = (select id from Queues
                               where project = ? and name = ?)
            ''', metadata['ttl'], id, project, queue)

            if not self.driver.affected:
                raise exceptions.ClaimDoesNotExist(claim_id,
                                                   queue,
                                                   project)

            self.__update_claimed(id, metadata['ttl'])

    def __update_claimed(self, cid, ttl):
        # Precondition: cid is not expired
        self.driver.run('''
            update Messages
               set created = julianday() * 86400.0,
                   ttl = ?
             where ttl < ?
               and id in (select msgid from Locked
                           where cid = ?)
        ''', ttl, ttl, cid)

    def delete(self, queue, claim_id, project):
        if project is None:
            project = ''

        try:
            cid = utils.cid_decode(claim_id)
        except exceptions.MalformedID:
            return

        self.driver.run('''
            delete from Claims
             where id = ?
               and qid = (select id from Queues
                           where project = ? and name = ?)
        ''', cid, project, queue)
