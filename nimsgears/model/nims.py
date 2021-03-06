import os
import re
import shutil
import hashlib
import datetime

import dicom
import transaction
from elixir import *

import nimsutil
from nimsgears.model import metadata, DBSession
from repoze.what import predicates

from tg import session
from sqlalchemy.util._collections import NamedTuple

__session__ = DBSession
__metadata__ = metadata

__all__  = ['Group', 'User', 'Permission', 'Message', 'Job', 'Access', 'AccessPrivilege']
__all__ += ['ResearchGroup', 'Person', 'Subject', 'DataContainer', 'Experiment', 'Session', 'Epoch']
__all__ += ['Dataset', 'PrimaryMRData', 'DicomData' , 'GEPFile', 'NiftiData']


class Group(Entity):

    """Group definition for :mod:`repoze.what`; `group_name` required."""

    gid = Field(Unicode(32), unique=True)           # translation for group_name set in app_cfg.py
    name = Field(Unicode(255))
    created = Field(DateTime, default=datetime.datetime.now)

    users = ManyToMany('User', onupdate='CASCADE', ondelete='CASCADE')
    permissions = ManyToMany('Permission', onupdate='CASCADE', ondelete='CASCADE')

    def __repr__(self):
        return ('<Group: group_id=%s>' % self.gid).encode('utf-8')

    def __unicode__(self):
        return self.name

    @classmethod
    def by_gid(cls, gid):
        return cls.query.filter_by(gid=gid).one()


class User(Entity):

    """User definition for :mod:`repoze.who`; `user_name` required."""

    uid = Field(Unicode(32), unique=True)           # translation for user_name set in app_cfg.py
    firstname = Field(Unicode(255))
    lastname = Field(Unicode(255))
    email = Field(Unicode(255), info={'rum': {'field':'Email'}})
    _password = Field(Unicode(128), colname='password', info={'rum': {'field':'Password'}}, synonym='password')
    created = Field(DateTime, default=datetime.datetime.now)
    admin_mode = Field(Boolean, default=False)

    groups = ManyToMany('Group', onupdate='CASCADE', ondelete='CASCADE')

    accesses = OneToMany('Access')
    research_groups = ManyToMany('ResearchGroup', inverse='members')
    manager_groups = ManyToMany('ResearchGroup', inverse='managers')
    pi_groups = ManyToMany('ResearchGroup', inverse='pis')
    messages = OneToMany('Message', inverse='recipient')

    def __init__(self, **kwargs):
        if 'uid' in kwargs:
            ldap_firstname, ldap_lastname, ldap_email = nimsutil.ldap_query(kwargs['uid'])
            kwargs['firstname'] = ldap_firstname
            kwargs['lastname'] = ldap_lastname
            kwargs['email'] = ldap_email
        super(User, self).__init__(**kwargs)

    def __repr__(self):
        return (u'<User: %s, %s, %s>' % (self.uid, self.email, self.name)).encode('utf-8')

    def __unicode__(self):
        return self.name or self.uid

    @property
    def name(self):
        if self.firstname and self.lastname:
            return u'%s, %s' % (self.lastname, self.firstname)
        else:
            return self.lastname

    @property
    def displayname(self):
        if self.firstname and self.lastname:
            return u'%s %s' % (self.firstname, self.lastname)
        else:
            return self.uid

    @property
    def is_superuser(self):
        """Return True if user is a superuser and has admin mode enabled."""
        return predicates.in_group('superusers') and self.admin_mode

    @classmethod
    def by_email(cls, email):
        return cls.query.filter_by(email=email).first()

    @classmethod
    def by_uid(cls, uid, create=False, password=None):
        user = cls.query.filter_by(uid=uid).first()
        if not user and create:
            user = cls(uid=uid, password=(password or uid))
        return user

    @staticmethod
    def _hash_password(password):
        # Make sure password is a str because we cannot hash unicode objects
        if isinstance(password, unicode):
            password = password.encode('utf-8')
        salt = hashlib.sha256()
        salt.update(os.urandom(60))
        hash = hashlib.sha256()
        hash.update(password + salt.hexdigest())
        password = salt.hexdigest() + hash.hexdigest()
        # Make sure the hashed password is a unicode object at the end of the
        # process because SQLAlchemy _wants_ unicode objects for Unicode cols
        if not isinstance(password, unicode):
            password = password.decode('utf-8')
        return password

    def _set_password(self, password):
        """Hash ``password`` on the fly and store its hashed version."""
        self._password = self._hash_password(password)
    def _get_password(self):
        """Return the hashed version of the password."""
        return self._password
    password = property(_get_password, _set_password)

    def validate_password(self, password):
        """Check the password against existing credentials."""
        hash = hashlib.sha256()
        if isinstance(password, unicode):
            password = password.encode('utf-8')
        hash.update(password + str(self.password[:64]))
        return self.password[64:] == hash.hexdigest()

    @property
    def permissions(self):
        """Return a set with all permissions granted to the user."""
        perms = set()
        for g in self.groups:
            perms = perms | set(g.permissions)
        return perms

    @property
    def unread_msg_cnt(self):
        return len([msg for msg in self.messages if not msg.read]) if self.messages else 0

    @property
    def dataset_cnt(self):
        query = DBSession.query(Session)
        if not self.is_superuser:
            query = query.join(Subject, Session.subject).join(Experiment, Subject.experiment).join(Access).filter(Access.user==self)
        return query.count()

    @property
    def job_cnt(self):
        return Job.query.filter((Job.status == u'new') | (Job.status == u'active')).count()

    def get_trash_flag(self):
        trash_flag = session.get(self.uid, 0)
        return trash_flag

    def _filter_query(self, query, with_privilege=None):
        query = query.add_entity(Access).join(Access).filter(Access.user == self)
        if with_privilege:
            query = query.filter(Access.privilege >= AccessPrivilege.value(with_privilege))
        return query

    def has_access_to(self, element, with_privilege=None):
        if isinstance(element, Experiment):
            query = Experiment.query
        elif isinstance(element, Session):
            query = (Session.query
                .join(Subject, Session.subject)
                .join(Experiment, Subject.experiment))
        elif isinstance(element, Epoch):
            query = (Epoch.query
                .join(Session, Epoch.session)
                .join(Subject, Session.subject)
                .join(Experiment, Subject.experiment))
        elif isinstance(element, Dataset):
            query = (Dataset.query
                .filter(Dataset.id == element.id)
                .join(Epoch)
                .join(Session, Epoch.session)
                .join(Subject, Session.subject)
                .join(Experiment, Subject.experiment))
        result = query._filter_query(query, with_privilege).first()
        return result != None


    def get_experiments(self, including_trash=None, only_trash=None, contains_trash=None, with_privilege=None):
        if not (including_trash or only_trash or contains_trash):
            trash_flag = self.get_trash_flag()
            if trash_flag == 1:
                including_trash = True
            elif trash_flag == 2:
                contains_trash = True

        query = Experiment.query

        if including_trash:
            pass # including everything, so no filter necessary
        elif only_trash or contains_trash:
            query = (query
                .join(Subject, Experiment.subjects)
                .join(Session, Subject.sessions)
                .join(Epoch, Session.epochs)
                .join(Dataset, Epoch.datasets))
            if only_trash:
                query = (query
                    .filter(Experiment.trashtime != None))
            else: # contains_trash
                query = (query
                    .filter((Experiment.trashtime != None) |
                            (Session.trashtime != None) |
                            (Epoch.trashtime != None) |
                            (Dataset.trashtime != None)))
        else: # no trash
            query = (query
                .filter(Experiment.trashtime == None))

        result_dict = {}
        if self.is_superuser:
            unfiltered_results = query.all()
            for result in unfiltered_results:
                result_dict[result.id] = result

        filtered_results = self._filter_query(query, with_privilege).all()
        for result in filtered_results:
            result_dict[result.Experiment.id] = result

        # Since these don't hit the filter, and thus don't get access privileges appended to them, we add them here
        if self.is_superuser:
            for key, value in result_dict.iteritems():
                if not isinstance(value, NamedTuple):
                    result_dict[key] = NamedTuple([value, None], ['Experiment', 'Access'])

        return result_dict

    def get_sessions(self, by_experiment_id=None, including_trash=False, only_trash=False, contains_trash=False, with_privilege=None):
        if not (including_trash or only_trash or contains_trash):
            trash_flag = self.get_trash_flag()
            if trash_flag == 1:
                including_trash = True
            elif trash_flag == 2:
                contains_trash = True

        query = (Session.query
            .join(Subject, Session.subject)
            .join(Experiment, Subject.experiment))

        if by_experiment_id:
            query = (query
                .filter(Experiment.id == by_experiment_id))

        if including_trash:
            pass # including everything, so no filter necessary
        elif contains_trash or only_trash:
            query = (query
                .join(Epoch, Session.epochs)
                .join(Dataset, Epoch.datasets))
            if only_trash:
                query = (query
                    .filter(Session.trashtime != None))
            else: # contains_trash
                query = (query
                    .filter((Session.trashtime != None) |
                            (Epoch.trashtime != None) |
                            (Dataset.trashtime != None)))
        else: # no trash
            query = (query
                .filter(Session.trashtime == None))

        result_dict = {}
        if self.is_superuser:
            unfiltered_results = query.all()
            for result in unfiltered_results:
                result_dict[result.id] = result

        filtered_results = self._filter_query(query, with_privilege).all()
        for result in filtered_results:
            result_dict[result.Session.id] = result

        # Since these don't hit the filter, and thus don't get access
        # privileges appended to them, we add them
        if self.is_superuser:
            for key, value in result_dict.iteritems():
                if not isinstance(value, NamedTuple):
                    result_dict[key] = NamedTuple([value, None], ['Session', 'Access'])

        return result_dict

    def get_epochs(self, by_experiment_id=None, by_session_id=None, including_trash=False, only_trash=False, contains_trash=False, with_privilege=None):
        if not (including_trash or only_trash or contains_trash):
            trash_flag = self.get_trash_flag()
            if trash_flag == 1:
                including_trash = True
            elif trash_flag == 2:
                contains_trash = True

        query = (Epoch.query
            .join(Session, Epoch.session))

        if by_session_id:
            query = (query
                .filter(Session.id == by_session_id))

        query = (query
            .join(Subject, Session.subject)
            .join(Experiment, Subject.experiment))

        if by_experiment_id:
            query = (query
                .filter(Experiment.id == by_experiment_id))

        if including_trash:
            pass # including everything, so no filter necessary
        elif only_trash or contains_trash:
            query = (query
                .join(Dataset, Epoch.datasets))
            if only_trash:
                query = (query
                    .filter(Epoch.trashtime != None))
            else: # contains_trash
                query = (query
                    .filter((Epoch.trashtime != None) |
                            (Dataset.trashtime != None)))
        else: # no trash
            query = (query
                .filter(Epoch.trashtime == None))

        result_dict = {}
        if self.is_superuser:
            unfiltered_results = query.all()
            for result in unfiltered_results:
                result_dict[result.id] = result

        filtered_results = self._filter_query(query, with_privilege).all()
        for result in filtered_results:
            result_dict[result.Epoch.id] = result

        # Since these don't hit the filter, and thus don't get access
        # privileges appended to them, we add them
        if self.is_superuser:
            for key, value in result_dict.iteritems():
                if not isinstance(value, NamedTuple):
                    result_dict[key] = NamedTuple([value, None], ['Epoch', 'Access'])

        return result_dict

    def get_datasets(self, by_experiment_id=None, by_session_id=None, by_epoch_id=None, including_trash=False, only_trash=False, contains_trash=False, with_privilege=None):
        if not (including_trash or only_trash or contains_trash):
            trash_flag = self.get_trash_flag()
            if trash_flag == 1:
                including_trash = True
            elif trash_flag == 2:
                contains_trash = True

        query = (Dataset.query
            .join(Epoch))

        if by_epoch_id:
            query = (query
                .filter(Epoch.id == by_epoch_id))

        query = (query
            .join(Session, Epoch.session))

        if by_session_id:
            query = (query
                .filter(Session.id == by_session_id))

        query = (query
            .join(Subject, Session.subject)
            .join(Experiment, Subject.experiment))

        if by_experiment_id:
            query = (query
                .filter(Experiment.id == by_experiment_id))

        if including_trash:
            pass # including everything, so no filter necessary
        elif only_trash or contains_trash:
            query = (query
                .filter(Dataset.trashtime != None))
        else: # no trash
            query = (query
                .filter(Dataset.trashtime == None))

        result_dict = {}
        if self.is_superuser:
            unfiltered_results = query.all()
            for result in unfiltered_results:
                result_dict[result.id] = result

        filtered_results = self._filter_query(query, with_privilege).all()
        for result in filtered_results:
            result_dict[result.Dataset.id] = result

        # Since these don't hit the filter, and thus don't get access
        # privileges appended to them, we add them
        if self.is_superuser:
            for key, value in result_dict.iteritems():
                if not isinstance(value, NamedTuple):
                    result_dict[key] = NamedTuple([value, None], ['Dataset', 'Access'])

        return result_dict

class Permission(Entity):

    """Permission definition for :mod:`repoze.what`; `permission_name` required."""

    pid = Field(Unicode(32), unique=True)           # translation for user_name set in app_cfg.py
    name = Field(Unicode(255))

    groups = ManyToMany('Group', onupdate='CASCADE', ondelete='CASCADE')

    def __repr__(self):
        return ('<Permission: name=%s>' % self.pid).encode('utf-8')

    def __unicode__(self):
        return self.pid


class Message(Entity):

    subject = Field(Unicode(255), required=True)
    body = Field(Unicode)
    priority = Field(Enum(u'normal', u'high', name=u'priority'), default=u'normal')
    created = Field(DateTime, default=datetime.datetime.now)
    read = Field(DateTime)

    recipient = ManyToOne('User', inverse='messages')

    def __repr__(self):
        return ('<Message: "%s: %s", prio=%s>' % (self.recipient, self.subject, self.priority)).encode('utf-8')

    def __unicode__(self):
        return u'%s: %s' % (self.recipient, self.subject)


class Job(Entity):

    timestamp = Field(DateTime, default=datetime.datetime.now)
    status = Field(Enum(u'new', u'active', u'done', u'failed', name=u'status'), default=u'new')
    task = Field(Enum(u'find', u'proc', name=u'task'))
    redo_all = Field(Boolean, default=False)
    progress = Field(Integer)
    activity = Field(Unicode(255))

    data_container = ManyToOne('DataContainer', inverse='jobs')

    def __init__(self, **kwargs):
        super(Job, self).__init__(**kwargs)
        if 'nims_path' in kwargs:
            self.restart(kwargs['nims_path'])

    def __unicode__(self):
        return u'%s#%s' % (self.data_container, self.task)

    def restart(self, nims_path):
        self.status = u'new'
        query = Dataset.query.filter(Dataset.container == self.data_container)
        if self.task == u'find':
            query = query.filter_by(kind=u'secondary')
        elif self.task == u'proc':
            query = query.filter_by(kind=u'derived')
        for ds in query.all():
            shutil.rmtree(os.path.join(nims_path, ds.relpath))
            ds.delete()


class AccessPrivilege(object):

    privilege_names = {
        1: (u'Anon-Read'),
        2: (u'Read-Only'),
        3: (u'Read-Write'),
        4: (u'Manage'),
        }

    privilege_values = dict((i[1],i[0]) for i in privilege_names.iteritems())

    @classmethod
    def name(cls, priv):
        return cls.privilege_names[priv] if priv in cls.privilege_names else None

    @classmethod
    def names(cls):
        return cls.privilege_names.values()

    @classmethod
    def value(cls, priv):
        return cls.privilege_values[priv] if priv in cls.privilege_values else None

    @classmethod
    def values(cls):
        return cls.privilege_values.values()


class Access(Entity):

    user = ManyToOne('User')
    experiment = ManyToOne('Experiment')
    privilege = Field(Integer)

    def __init__(self, **kwargs):
        if 'privilege_name' in kwargs:
            kwargs['privilege'] = AccessPrivilege.value(kwargs['privilege_name'])
        super(Access, self).__init__(**kwargs)

    def __unicode__(self):
        return u'%s: (%s, %s)' % (AccessPrivilege.name(self.privilege), self.user, self.experiment)


class ResearchGroup(Entity):

    gid = Field(Unicode(32), unique=True)
    name = Field(Unicode(255))

    pis = ManyToMany('User', inverse='pi_groups')
    managers = ManyToMany('User', inverse='managers')
    members = ManyToMany('User', inverse='research_groups')

    experiments = OneToMany('Experiment')

    def __repr__(self):
        return (u'<%s: %s>' % (self.__class__.__name__, self.gid)).encode('utf-8')

    def __unicode__(self):
        return self.name or self.gid

    @classmethod
    def get_all_ids(cls):
        return [rg.gid for rg in cls.query.all()]


class Person(Entity):

    roles = OneToMany('Subject')

    @property
    def experiments(self):
        return [r.experiment for r in roles]


class DataContainer(Entity):

    using_options(inheritance='multi')

    timestamp = Field(DateTime, default=datetime.datetime.now)
    duration = Field(Interval, default=datetime.timedelta())
    trashtime = Field(DateTime)
    updated = Field(Boolean, default=False, index=True)
    needs_finding = Field(Boolean, default=False, index=True)
    needs_processing = Field(Boolean, default=False, index=True)

    datasets = OneToMany('Dataset')
    jobs = OneToMany('Job')

    def __repr__(self):
        return (u'<%s: %s>' % (self.__class__.__name__, self.name)).encode('utf-8')

    @property
    def primary_dataset(self):
        return Dataset.query.filter_by(container=self).filter_by(kind=u'primary').first()


class Experiment(DataContainer):

    using_options(inheritance='multi')

    name = Field(Unicode(63))
    irb = Field(Unicode(16))

    owner = ManyToOne('ResearchGroup', required=True)
    accesses = OneToMany('Access')
    subjects = OneToMany('Subject')

    def __unicode__(self):
        return u'%s/%s' % (self.owner, self.name)

    @classmethod
    def from_owner_name(cls, owner, name):
        experiment = cls.query.filter_by(owner=owner).filter_by(name=name).first()
        if not experiment:
            experiment = cls(owner=owner, name=name)
            for manager in set(owner.managers + owner.pis):                         # managers & PIs
                Access(experiment=experiment, user=manager, privilege_name=u'Manage')
            for member in set(owner.members) - set(owner.managers + owner.pis):     # other members
                Access(experiment=experiment, user=member, privilege_name=u'Read-Only')
        return experiment

    @property
    def persons(self):
        return [s.person for s in self.subject]

    @property
    def is_trash(self):
        return bool(self.trashtime)

    @property
    def contains_trash(self):
        if self.is_trash:
            return True
        for subject in self.subjects:
            if subject.contains_trash:
                return True
        return False

    def trash(self, trashtime=datetime.datetime.now()):
        self.trashtime = trashtime
        for subject in self.subjects:
            subject.trash(trashtime)

    def untrash(self):
        if self.is_trash:
            self.trashtime = None
            for subject in self.subjects:
                subject.untrash()

class Subject(DataContainer):

    using_options(inheritance='multi')

    code = Field(Unicode(31))
    firstname = Field(Unicode(63))
    lastname = Field(Unicode(63))
    dob = Field(Date)
    #consent_form = Field(Unicode(63))

    experiment = ManyToOne('Experiment')
    person = ManyToOne('Person')
    sessions = OneToMany('Session')

    def __unicode__(self):
        return u'%s, %s' % (self.lastname, self.firstname)

    @classmethod
    def from_metadata(cls, md):
        query = cls.query.join(Experiment, cls.experiment).filter(Experiment.name==md.exp_name)
        if md.subj_code:
            subject = query.filter(cls.code==md.subj_code).first()
        elif md.subj_fn and md.subj_ln:
            subject = query.filter(cls.firstname==md.subj_fn).filter(cls.lastname==md.subj_ln).filter(cls.dob==md.subj_dob).first()
        else:
            subject = None
        if not subject:
            owner = ResearchGroup.query.filter_by(gid=md.group_name).one()
            experiment = Experiment.from_owner_name(owner, md.exp_name)
            if not md.subj_code:
                code_num = max([int('0%s' % re.sub(r'[^0-9]+', '', s.code)) for s in experiment.subjects]) + 1 if experiment.subjects else 1
                md.subj_code = u's%03d' % code_num
            subject = cls(
                    experiment=experiment,
                    person=Person(),
                    code=md.subj_code,
                    firstname=md.subj_fn,
                    lastname=md.subj_ln,
                    dob=md.subj_dob,
                    )
        return subject

    @property
    def name(self):
        return u'%s, %s' % (self.lastname, self.firstname)

    @property
    def is_trash(self):
        return bool(self.trashtime)

    @property
    def contains_trash(self):
        if self.is_trash:
            return True
        for session in self.sessions:
            if session.contains_trash:
                return True
        return False

    def trash(self, trashtime=datetime.datetime.now()):
        self.trashtime = trashtime
        for session in self.sessions:
            session.trash(trashtime)

    def untrash(self):
        if self.is_trash:
            self.trashtime = None
            self.experiment.untrash()
            for session in self.sessions:
                session.untrash()


class Session(DataContainer):

    using_options(inheritance='multi')

    uid = Field(Binary(32), index=True)
    exam = Field(Integer)
    notes = Field(Unicode)

    subject = ManyToOne('Subject')
    operator = ManyToOne('User')
    epochs = OneToMany('Epoch')

    def __unicode__(self):
        return u'Session'

    @classmethod
    def from_metadata(cls, md):
        session = cls.query.filter_by(uid=md.exam_uid).first()
        if not session:
            subject = Subject.from_metadata(md)
            operator = None # FIXME: set operator to an actual user, creating user if necessary
            session = Session(uid=md.exam_uid, exam=md.exam_no, subject=subject, operator=operator)
        return session

    @property
    def name(self):
        return u'%s_%d' % (self.timestamp.strftime(u'%Y%m%d_%H%M'), self.exam)

    @property
    def is_trash(self):
        return bool(self.trashtime)

    @property
    def contains_trash(self):
        if self.is_trash:
            return True
        for epoch in self.epochs:
            if epoch.contains_trash:
                return True
        return False

    def trash(self, trashtime=datetime.datetime.now()):
        self.trashtime = trashtime
        for epoch in self.epochs:
            epoch.trash(trashtime)

    def untrash(self):
        if self.is_trash:
            self.trashtime = None
            self.subject.untrash()
            for epoch in self.epochs:
                epoch.untrash()

class Epoch(DataContainer):

    using_options(inheritance='multi')

    uid = Field(Binary(32), index=True)
    series = Field(Integer)
    acq = Field(Integer, index=True)
    description = Field(Unicode(255))
    psd = Field(Unicode(255))
    physio_flag = Field(Boolean, default=False)

    tr = Field(Float)
    te = Field(Float)

    session = ManyToOne('Session')

    def __unicode__(self):
        return u'Epoch %s %s' % (self.session.subject.experiment, self.timestamp.strftime('%Y-%m-%d %H:%M:%S'))

    @classmethod
    def from_metadata(cls, md):
        epoch = cls.query.filter_by(uid=md.series_uid).filter_by(acq=md.acq_no).first()
        if not epoch:
            session = Session.from_metadata(md)
            if session.timestamp is None or session.timestamp > md.timestamp:
                session.timestamp = md.timestamp
            epoch = cls(
                    session=session,
                    timestamp=md.timestamp,
                    duration=md.duration,
                    uid=md.series_uid,
                    series=md.series_no,
                    acq=md.acq_no,
                    description=md.series_desc,
                    psd=md.psd_name,
                    physio_flag = md.physio_flag and u'epi' in md.psd_name.lower(),
                    )
        return epoch

    @property
    def name(self):
        return '%s_%d_%d_%s' % (self.timestamp.strftime('%H%M%S'), self.series, self.acq, self.description)

    @property
    def is_trash(self):
        return bool(self.trashtime)

    @property
    def contains_trash(self):
        if self.is_trash:
            return True
        for dataset in self.datasets:
            if dataset.is_trash:
                return True
        return False

    def trash(self, trashtime=datetime.datetime.now()):
        self.trashtime = trashtime
        for dataset in self.datasets:
            dataset.trash(trashtime)

    def untrash(self):
        if self.is_trash:
            self.trashtime = None
            self.session.untrash()
            for dataset in self.datasets:
                dataset.untrash()


class Dataset(Entity):

    offset = Field(Interval, default=datetime.timedelta())
    trashtime = Field(DateTime)
    kind = Field(Enum(u'primary', u'secondary', u'derived', name=u'kind'), default=u'derived')
    datatype = Field(Unicode(63))
    _updatetime = Field(DateTime, default=datetime.datetime.now, colname='updatetime', synonym='updatetime')
    digest = Field(Binary(20))
    compressed = Field(Boolean, default=False, index=True)
    archived = Field(Boolean, default=False, index=True)
    file_cnt_act = Field(Integer)
    file_cnt_tgt = Field(Integer)

    container = ManyToOne('DataContainer')
    parents = ManyToMany('Dataset')

    def __repr__(self):
        return (u'<%s: %s>' % (self.__class__.__name__, self.datatype)).encode('utf-8')

    def __unicode__(self):
        return u'<%s %s>' % (self.__class__.__name__, self.container)

    @classmethod
    def at_path(cls, nims_path, filename=None, datatype=None, archived=False):
        dataset = cls(datatype=datatype, archived=archived)
        transaction.commit()
        DBSession.add(dataset)
        nimsutil.make_joined_path(nims_path, dataset.relpath)
        return dataset

    @property
    def name(self):
        return nimsutil.clean_string(self.datatype)

    @property
    def relpath(self):
        return '%s/%03d/%08d' % ('archive' if self.archived else 'data', self.id % 1000, self.id)

    def _get_updatetime(self):
        return self._updatetime
    def _set_updatetime(self, updatetime):
        self._updatetime = updatetime
        self.container.updated = True
    updatetime = property(_get_updatetime, _set_updatetime)

    @property
    def is_trash(self):
        return bool(self.trashtime)

    @property
    def contains_trash(self):
        return self.is_trash

    def trash(self, trashtime=datetime.datetime.now()):
        self.trashtime = trashtime

    def untrash(self):
        if self.is_trash:
            self.trashtime = None
            self.container.untrash()

    def update_file_cnt_and_digest(self, nims_path):
        old_digest = self.digest
        new_hash = hashlib.sha1()
        filelist = os.listdir(os.path.join(nims_path, self.relpath))
        for filename in sorted(filelist):
            with open(os.path.join(nims_path, self.relpath, filename), 'rb') as fd:
                for chunk in iter(lambda: fd.read(1048576 * new_hash.block_size), ''):
                    new_hash.update(chunk)
        self.digest = new_hash.digest()
        self.file_cnt_act = len(filelist)
        return self.digest != old_digest


class PrimaryMRData(Dataset):

    """Abstract superclass to all MRI data types."""

    priority = 0
    filename_ext = ''

    @classmethod
    def at_path(cls, nims_path, filename, datatype=None, archived=False):
        metadata = cls.get_metadata(filename)
        if metadata:
            dataset = cls.from_metadata(metadata)
            nimsutil.make_joined_path(nims_path, dataset.relpath)
        else:
            dataset = None
        return dataset

    @classmethod
    def from_metadata(cls, md):
        dataset = cls.query.join(Epoch).filter(Epoch.uid == md.series_uid).filter(Epoch.acq == md.acq_no).with_lockmode('update').first()
        if not dataset:
            epoch = Epoch.from_metadata(md)
            dataset = cls(
                    container=epoch,
                    datatype=md.datatype,
                    kind=u'primary',
                    archived=True,
                    )
            transaction.commit()
            DBSession.add(dataset)
        return dataset


class DicomData(PrimaryMRData):

    priority = 0
    filename_ext = '.dcm'

    @staticmethod
    def get_metadata(filename):
        try:
            dcm = nimsutil.dicomutil.DicomFile(filename)
        except nimsutil.dicomutil.DicomError:
            md = None
        else:
            md = Metadata()
            md.datatype = u'Dicom Files'
            md.exam_no = dcm.exam_no
            md.series_no = dcm.series_no
            md.acq_no = dcm.acq_no
            md.exam_uid = nimsutil.pack_dicom_uid(dcm.exam_uid)
            md.series_uid = nimsutil.pack_dicom_uid(dcm.series_uid)
            md.psd_name = unicode(dcm.psd_name)
            md.physio_flag = dcm.physio_flag
            md.series_desc = nimsutil.clean_string(dcm.series_desc)
            md.timestamp = dcm.timestamp
            md.duration = dcm.duration
            md.subj_code, md.subj_fn, md.subj_ln, md.subj_dob = nimsutil.parse_subject(dcm.patient_name, dcm.patient_dob)
            md.group_name, md.exp_name = nimsutil.parse_patient_id(dcm.patient_id, ResearchGroup.get_all_ids())
        return md


class GEPFile(PrimaryMRData):

    priority = 1

    @staticmethod
    def get_metadata(filename):
        try:
            pf = nimsutil.pfile.PFile(filename)
        except nimsutil.pfile.PFileError:
            md = None
        else:
            md = Metadata()
            md.datatype = u'GE PFile'
            md.exam_no = pf.exam_no
            md.series_no = pf.series_no
            md.acq_no = pf.acq_no
            md.exam_uid = nimsutil.pack_dicom_uid(pf.exam_uid)
            md.series_uid = nimsutil.pack_dicom_uid(pf.series_uid)
            md.psd_name = unicode(pf.psd_name)
            md.physio_flag = pf.physio_flag
            md.series_desc = nimsutil.clean_string(pf.series_desc)
            md.timestamp = pf.timestamp
            md.duration = pf.duration
            all_groups = [rg.id for rg in ResearchGroup.query.all()]
            md.subj_code, md.subj_fn, md.subj_ln, md.subj_dob = nimsutil.parse_subject(pf.patient_name, pf.patient_dob)
            md.group_name, md.exp_name = nimsutil.parse_patient_id(pf.patient_id, ResearchGroup.get_all_ids())
        return md


class NiftiData(Dataset):

    pass


class Metadata(object):

        pass
