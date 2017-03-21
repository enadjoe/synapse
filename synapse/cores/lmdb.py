from __future__ import absolute_import

import sys
import struct
from binascii import hexlify, unhexlify
from contextlib import contextmanager

import msgpack
import xxhash

import synapse.cores.common as s_cores_common
from synapse.common import genpath, msgenpack, msgunpack
import synapse.compat as s_compat
import synapse.datamodel as s_datamodel
import synapse.lib.threads as s_threads

import lmdb

# File conventions:
# i, p, v, t: iden, prop, value, timestamp
# i_enc, p_enc, v_key_enc, t_enc: above encoded to be space efficient and fully-ordered when
# compared lexicographically (i.e. 'aaa' < 'ba')
# pk:  primary key, unique identifier of the row in the main table
# pk_val_enc:  space efficient encoding of pk for use as value in an index table
# pk_key_enc:  database-efficient encoding of pk for use as key in main table

# N.B.  LMDB calls the separate namespaces in a file "databases" (e.g. named parameter db=).

# Largest primary key value.  No more rows than this
MAX_PK = sys.maxsize

# Bytes in MAX_PK
MAX_PK_BYTES = 8 if sys.maxsize > 2**32 else 4

# Prefix to indicate that a v is a negative value and not the default nonnegative value
NEGATIVE_VAL_MARKER = -1

# Prefix to indicate than a v is a string
STRING_VAL_MARKER = -2

# Prefix to indicate that a v is hash of a string
HASH_VAL_MARKER = -3

# The negative marker encoded
NEGATIVE_VAL_MARKER_ENC = msgenpack(NEGATIVE_VAL_MARKER)

# The string marker encoded
STRING_VAL_MARKER_ENC = msgenpack(STRING_VAL_MARKER)

# The hash marker encoded
HASH_VAL_MARKER_ENC = msgenpack(HASH_VAL_MARKER)

# Number of bytes in a UUID
UUID_SIZE = 16

MAX_UUID_PLUS_1 = 2**(UUID_SIZE*8)

# An index key can't ever be larger (lexicographically) than this
MAX_INDEX_KEY = b'\xff' * 20

# String vals of this size or larger will be truncated and hashed in index
LARGE_STRING_SIZE = 128

# Largest length allowed for a prop
MAX_PROP_LEN = 350

# Matches sqlite3
MAX_INT_VAL = 2 ** 63 - 1
MIN_INT_VAL = -1 * (2 ** 63)

# The maximum possible timestamp.  Probably a bit overkill
MAX_TIME_ENC = msgenpack(MAX_INT_VAL)


class DatabaseInconsistent(Exception):
    ''' If you get this Exception, that means the database is corrupt '''
    pass


class DatabaseLimitReached(Exception):
    ''' You've reached some limit of the database '''
    pass


# Python 2.7 version of lmdb buffers=True functions return buffers.  Python 3 version returns
# memoryview
if (sys.version_info > (3, 0)):
    def memToBytes(x):
        return x.tobytes()
else:
    def memToBytes(x):
        return str(x)


def _enc_val_key(v):
    ''' Encode a v.  Non-negative numbers are msgpack encoded.  Negative numbers are encoded
        as a marker, then the encoded negative of that value, so that the ordering of the
        encodings is easily mapped to the ordering of the negative numbers.  Note that this
        scheme prevents interleaving of value types:  all string encodings compare larger than
        all negative number encodings compare larger than all nonnegative encodings.  '''
    if s_compat.isint(v):
        if v >= 0:
            return msgenpack(v)
        else:
            return NEGATIVE_VAL_MARKER_ENC + msgenpack(-v)
    else:
        if len(v) >= LARGE_STRING_SIZE:
            return (HASH_VAL_MARKER_ENC + msgenpack(xxhash.xxh64(v).intdigest()))
        else:
            return STRING_VAL_MARKER_ENC + msgenpack(v)


def _enc_val_val(v):
    ''' Encode a v for use on the value side.  '''
    return msgenpack(v)


def _dec_val_val(unpacker):
    ''' Inverse of above '''
    return unpacker.unpack()


def _enc_iden(iden):
    ''' Encode an iden '''
    return unhexlify(iden)


def _dec_iden(iden_enc):
    ''' Decode an iden '''
    return hexlify(iden_enc).decode('utf8')


# The precompiled struct parser for native size_t
_SIZET_ST = struct.Struct('@Q' if sys.maxsize > 2**32 else '@L')


def _enc_pk_key(pk):
    ''' Encode for integerkey row DB option:  as a native size_t '''
    return _SIZET_ST.pack(pk)


def _dec_pk_key(pk_enc):
    ''' Inverse of above '''
    return _SIZET_ST.unpack(pk_enc)[0]


class CoreXact(s_cores_common.CoreXact):

    def _coreXactInit(self):
        self.txn = None

    def _coreXactCommit(self):
        self.txn.commit()

    def _coreXactBegin(self):
        self.txn = self.core.dbenv.begin(buffers=True, write=True)

    def _coreXactAcquire(self):
        pass

    def _coreXactRelease(self):
        pass


class Cortex(s_cores_common.Cortex):

    def _initCortex(self):
        self._initDbConn()

        self.initSizeBy('ge', self._sizeByGe)
        self.initRowsBy('ge', self._rowsByGe)

        self.initSizeBy('le', self._sizeByLe)
        self.initRowsBy('le', self._rowsByLe)
        self.initSizeBy('lt', self._sizeByLt)

        # use helpers from base class
        self.initRowsBy('gt', self._rowsByGt)
        self.initRowsBy('lt', self._rowsByLt)

        self.initSizeBy('range', self._sizeByRange)
        self.initRowsBy('range', self._rowsByRange)

    def _initDbInfo(self):
        name = self._link[1].get('path')[1:]
        if not name:
            raise Exception('No Path Specified!')

        if name.find(':') == -1:
            name = genpath(name)

        return {'name': name}

    def _getCoreXact(self, size=None):
        return CoreXact(self, size=size)

    def _get_largest_pk(self):
        with self._get_txn() as txn, txn.cursor(self.rows) as cursor:
            if not cursor.last():
                return 0  # db is empty
            return _dec_pk_key(cursor.key())

    @contextmanager
    def _get_txn(self, write=False):
        ''' LMDB doesn't have the concept of store access without a transaction, so figure out
        whether there's already one open and use that, else make one.  If we found an existing
        transaction, this doesn't close it after leaving the context.  If we made one and the
        context is exited without exception, the transaction is committed. '''
        existing_xact = self._core_xacts.get(s_threads.iden())
        if existing_xact is not None:
            yield existing_xact.txn
        else:
            with self.dbenv.begin(buffers=True, write=write) as txn:
                yield txn

    def flush(self):
        ''' Flushs/syncs to disk '''
        self.dbenv.sync(True)

    def _initDbConn(self):
        dbinfo = self._initDbInfo()
        dbname = dbinfo.get('name')

        # MAX DB Size.  Must be < 2 GiB for 32-bit.  Can be big for 64-bit systems.
        MAP_SIZE = 1073741824 if MAX_PK_BYTES == 4 else 1099511627776  # a terabyte

        map_size = self._link[1].get('lmdb:mapsize', MAP_SIZE)
        map_size, _ = s_datamodel.getTypeFrob('int', map_size)

        # Maximum number of "databases", really tables.  We use 4 different tables (1 main plus
        # 3 indices)
        MAX_DBS = 4

        # flush system buffers to disk only once per transaction.  Set to False can lead to last
        # transaction loss, but not corruption

        metasync_val = self._link[1].get('lmdb:metasync', False)
        metasync, _ = s_datamodel.getTypeFrob('bool', metasync_val)

        sync_val = self._link[1].get('lmdb:sync', True)
        sync, _ = s_datamodel.getTypeFrob('bool', sync_val)

        # Write data directly to mapped memory
        WRITEMAP = True

        # Doesn't create a subdirectory for storage files
        SUBDIR = False

        # We can disable locking...
        DEFAULT_LOCK = True
        lock_val = self._link[1].get('lmdb:lock', DEFAULT_LOCK)
        lock, _ = s_datamodel.getTypeFrob('bool', lock_val)

        # Maximum simultaneous readers.
        MAX_READERS = 4
        max_readers = self._link[1].get('lmdb:maxreaders', MAX_READERS)
        max_readers, _ = s_datamodel.getTypeFrob('int', max_readers)
        if max_readers == 1:
            lock = False

        self.dbenv = lmdb.Environment(dbname, map_size=map_size, subdir=SUBDIR, metasync=metasync,
                                      writemap=WRITEMAP, max_readers=max_readers, max_dbs=MAX_DBS,
                                      sync=sync, lock=lock)

        # Make the main storage table, keyed by an incrementing counter, pk
        # LMDB has an optimization (integerkey) if all the keys in a table are unsigned size_t.
        self.rows = self.dbenv.open_db(key=b"rows", integerkey=True)  # pk -> i,p,v,t

        # Note there's another LMDB optimization ("dupfixed") we're not using that we could
        # in the index tables.  It would pay off if a large proportion of keys are duplicates.

        # Make the iden-prop index table, keyed by iden-prop, with value being a pk
        self.index_ip = self.dbenv.open_db(key=b"ip", dupsort=True)  # i,p -> pk

        # Make the iden-value-prop index table, keyed by iden-value-prop, with value being a pk
        self.index_pvt = self.dbenv.open_db(key=b"pvt", dupsort=True)  # p,v,t -> pk

        # Make the iden-timestamp index table, keyed by iden-timestamp, with value being a pk
        self.index_pt = self.dbenv.open_db(key=b"pt", dupsort=True)  # p, t -> pk

        # Put 1 max key sentinel at the end of each index table.  This avoids unfortunate behavior
        # where the cursor moves backwards after deleting the final record.
        with self._get_txn(write=True) as txn:
            for db in (self.index_ip, self.index_pvt, self.index_pt):
                txn.put(MAX_INDEX_KEY, b'', db=db)
                # One more sentinel for going backwards through the pvt table.
                txn.put(b'\x00', b'', db=self.index_pvt)

        # Find the largest stored pk.  We just track this in memory from now on.
        largest_pk = self._get_largest_pk()
        if largest_pk == MAX_PK:
            raise DatabaseLimitReached('Out of primary key values')

        self.next_pk = largest_pk + 1

        def onfini():
            self.dbenv.close()
        self.onfini(onfini)

    def _addRows(self, rows):
        next_pk = self.next_pk
        encs = []
        for i, p, v, t in rows:
            if next_pk > MAX_PK:
                raise DatabaseLimitReached('Out of primary key values')
            if len(p) > MAX_PROP_LEN:
                raise DatabaseLimitReached('Property length too large')
            i_enc = _enc_iden(i)
            p_enc = msgenpack(p)
            v_val_enc = _enc_val_val(v)
            v_key_enc = _enc_val_key(v)
            t_enc = msgenpack(t)
            pk_val_enc = msgenpack(next_pk)
            pk_key_enc = _enc_pk_key(next_pk)
            # idx        0      1         2         3       4          5           6
            encs.append((i_enc, p_enc, v_val_enc, t_enc, v_key_enc, pk_val_enc, pk_key_enc))
            next_pk += 1

        with self._get_txn(write=True) as txn:
            # an iterator of key, value pairs:  key=pk_key_enc, val=i_enc+p_enc+v_val_enc+t_enc
            kvs = ((x[6], x[0] + x[1] + x[2] + x[3]) for x in encs)
            consumed, added = txn.cursor(db=self.rows).putmulti(kvs, overwrite=False, append=True)
            if consumed != added or consumed != len(encs):
                # Will only fail if record already exists, which should never happen
                raise DatabaseInconsistent('unexpected pk in DB')

            kvs = ((x[0] + x[1], x[5]) for x in encs)
            txn.cursor(db=self.index_ip).putmulti(kvs, dupdata=True)
            kvs = ((x[1] + x[4] + x[3], x[5]) for x in encs)
            txn.cursor(db=self.index_pvt).putmulti(kvs, dupdata=True)
            kvs = ((x[1] + x[3], x[5]) for x in encs)
            txn.cursor(db=self.index_pt).putmulti(kvs, dupdata=True)

            # self.next_pk should be protected from multiple writers. Luckily lmdb write lock does
            # that for us.
            self.next_pk = next_pk

    def _getRowByPkValEnc(self, txn, pk_val_enc, do_delete=False):
        UUID_SIZE = 16
        pk = msgunpack(pk_val_enc)
        if do_delete:
            row = txn.pop(_enc_pk_key(pk), db=self.rows)
        else:
            row = txn.get(_enc_pk_key(pk), db=self.rows)
        if row is None:
            raise DatabaseInconsistent('Index val has no corresponding row')
        i = _dec_iden(row[:UUID_SIZE])
        unpacker = msgpack.Unpacker(use_list=False, encoding='utf8')
        unpacker.feed(row[UUID_SIZE:])
        p = unpacker.unpack()
        v = _dec_val_val(unpacker)
        t = unpacker.unpack()
        return (i, p, v, t)

    def _getRowsById(self, iden):
        ret = []
        iden_enc = _enc_iden(iden)
        with self._get_txn() as txn, txn.cursor(self.index_ip) as cursor:
            if not cursor.set_range(iden_enc):
                raise DatabaseInconsistent("Missing sentinel")
            for key, value in cursor:
                if key[:len(iden_enc)] != iden_enc:
                    return ret

                ret.append(self._getRowByPkValEnc(txn, value))
        raise DatabaseInconsistent("Missing sentinel")

    def _delRowsById(self, iden):
        i_enc = _enc_iden(iden)

        with self._get_txn(write=True) as txn, txn.cursor(self.index_ip) as cursor:
            # Get the first record => i_enc
            if not cursor.set_range(i_enc):
                raise DatabaseInconsistent("Missing sentinel")
            while True:
                # We don't use iterator here because the delete already advances to the next
                # record
                key, value = cursor.item()
                if key[:len(i_enc)] != i_enc:
                    return
                p_enc = memToBytes(key[len(i_enc):])
                # Need to copy out with tobytes because we're deleting
                pk_val_enc = memToBytes(value)

                if not cursor.delete():
                    raise Exception('Delete failure')
                self._delRowAndIndices(txn, pk_val_enc, i_enc=i_enc, p_enc=p_enc,
                                       delete_ip=False)

    def _delRowsByIdProp(self, iden, prop, valu=None):
        i_enc = _enc_iden(iden)
        p_enc = msgenpack(prop)
        first_key = i_enc + p_enc

        with self._get_txn(write=True) as txn, txn.cursor(self.index_ip) as cursor:
            # Retrieve and delete I-P index
            if not cursor.set_range(first_key):
                raise DatabaseInconsistent("Missing sentinel")
            while True:
                # We don't use iterator here because the delete already advances to the next
                # record
                key, value = cursor.item()
                if key[:len(first_key)] != first_key:
                    return
                # Need to copy out with tobytes because we're deleting
                pk_val_enc = memToBytes(value)

                # Delete the row and the other indices
                if not self._delRowAndIndices(txn, pk_val_enc, i_enc=i_enc, p_enc=p_enc,
                                              delete_ip=False, only_if_val=valu):
                    if not cursor.next():
                        raise DatabaseInconsistent("Missing sentinel")
                else:
                    if not cursor.delete():
                        raise Exception('Delete failure')

    def _delRowAndIndices(self, txn, pk_val_enc, i_enc=None, p_enc=None, v_key_enc=None, t_enc=None,
                          delete_ip=True, delete_pvt=True, delete_pt=True, only_if_val=None):
        ''' Deletes the row corresponding to pk_val_enc and the indices pointing to it '''
        i, p, v, t = self._getRowByPkValEnc(txn, pk_val_enc, do_delete=True)

        if only_if_val is not None and only_if_val != v:
            return False

        if delete_ip and i_enc is None:
            i_enc = _enc_iden(i)

        if p_enc is None:
            p_enc = msgenpack(p)

        if delete_pvt and v_key_enc is None:
            v_key_enc = _enc_val_key(v)

        if (delete_pvt or delete_pt) and t_enc is None:
            t_enc = msgenpack(t)

        if delete_ip:
            # Delete I-P index entry
            if not txn.delete(i_enc + p_enc, value=pk_val_enc, db=self.index_ip):
                raise DatabaseInconsistent("Missing I-P index")

        if delete_pvt:
            # Delete P-V-T index entry
            if not txn.delete(p_enc + v_key_enc + t_enc, value=pk_val_enc, db=self.index_pvt):
                raise DatabaseInconsistent("Missing P-V-T index")

        if delete_pt:
            # Delete P-T index entry
            if not txn.delete(p_enc + t_enc, value=pk_val_enc, db=self.index_pt):
                raise DatabaseInconsistent("Missing P-T index")

        return True

    def _getRowsByIdProp(self, iden, prop, valu=None):
        # For now not making a ipv index because multiple v for a given i,p are probably rare
        iden_enc = _enc_iden(iden)
        prop_enc = msgenpack(prop)

        first_key = iden_enc + prop_enc

        ret = []

        with self._get_txn() as txn, txn.cursor(self.index_ip) as cursor:
            if not cursor.set_range(first_key):
                raise DatabaseInconsistent("Missing sentinel")
            for key, value in cursor:
                if memToBytes(key) != first_key:
                    return ret
                row = self._getRowByPkValEnc(txn, value)
                if valu is not None and row[2] != valu:
                    continue
                ret.append(row)
        raise DatabaseInconsistent("Missing sentinel")

    def _getSizeByProp(self, prop, valu=None, limit=None, mintime=None, maxtime=None):
        return self._getRowsByProp(prop, valu, limit, mintime, maxtime, do_count_only=True)

    def _delRowsByProp(self, prop, valu=None, mintime=None, maxtime=None):
        self._getRowsByProp(prop, valu, mintime=mintime, maxtime=maxtime, do_delete_only=True)

    def _getRowsByProp(self, prop, valu=None, limit=None, mintime=None, maxtime=None,
                       do_count_only=False, do_delete_only=False):

        assert(not (do_count_only and do_delete_only))
        indx = self.index_pt if valu is None else self.index_pvt
        p_enc = msgenpack(prop)
        v_key_enc = b'' if valu is None else _enc_val_key(valu)
        v_is_hashed = valu is not None and (v_key_enc[0] == HASH_VAL_MARKER_ENC)
        mintime_enc = b'' if mintime is None else msgenpack(mintime)
        maxtime_enc = MAX_TIME_ENC if maxtime is None else msgenpack(maxtime)

        first_key = p_enc + v_key_enc + mintime_enc
        last_key = p_enc + v_key_enc + maxtime_enc

        ret = []
        count = 0

        with self._get_txn(write=do_delete_only) as txn, txn.cursor(indx) as cursor:
            if not cursor.set_range(first_key):
                raise DatabaseInconsistent("Missing sentinel")
            while True:
                key, value = cursor.item()
                if memToBytes(key) >= last_key:
                    break
                if do_delete_only:
                    # Have to save off pk_val_enc because is being deleted
                    pk_val_enc = memToBytes(value)

                    if not cursor.delete():
                        raise Exception('Delete failure')

                    self._delRowAndIndices(txn, pk_val_enc, p_enc=p_enc,
                                           delete_pt=(valu is not None),
                                           delete_pvt=(valu is None), only_if_val=valu)
                elif not do_count_only or v_is_hashed:
                    # If we hashed, we must double check that val actually matches in row
                    row = self._getRowByPkValEnc(txn, value)
                    if v_is_hashed:
                        if valu != row[2]:
                            continue
                    if not do_count_only:
                        ret.append(row)
                count += 1
                if limit is not None and count >= limit:
                    break
                if not do_delete_only:
                    # deleting auto-advances, so we don't advance the cursor
                    if not cursor.next():
                        raise DatabaseInconsistent('Missing sentinel')

        return count if do_count_only else ret

    # right_closed:  on an interval, e.g. (0, 1] is left-open and right-closed

    def _sizeByGe(self, prop, valu, limit=None):
        return self._rowsByMinmax(prop, valu, MAX_INT_VAL, limit, right_closed=True,
                                  do_count_only=True)

    def _rowsByGe(self, prop, valu, limit=None):
        return self._rowsByMinmax(prop, valu, MAX_INT_VAL, limit, right_closed=True)

    def _sizeByLe(self, prop, valu, limit=None):
        return self._rowsByMinmax(prop, MIN_INT_VAL, valu, limit, right_closed=True,
                                  do_count_only=True)

    def _sizeByLt(self, prop, valu, limit=None):
        return self._rowsByMinmax(prop, MIN_INT_VAL, valu, limit, right_closed=False,
                                  do_count_only=True)

    def _rowsByLe(self, prop, valu, limit=None):
        return self._rowsByMinmax(prop, MIN_INT_VAL, valu, limit, right_closed=True)

    def _sizeByRange(self, prop, valu, limit=None):
        return self._rowsByMinmax(prop, valu[0], valu[1], limit, do_count_only=True)

    def _rowsByRange(self, prop, valu, limit=None):
        return self._rowsByMinmax(prop, valu[0], valu[1], limit)

    def _rowsByMinmax(self, prop, minval, maxval, limit, right_closed=False, do_count_only=False):
        if minval > maxval:
            return 0
        do_neg_search = (minval < 0)
        do_pos_search = (maxval >= 0)
        ret = 0 if do_count_only else []

        p_enc = msgenpack(prop)

        # The encodings of negative integers and positive integers are not continuous, so we split
        # into two queries.  Also, the ordering of the encoding of negative integers is backwards.
        if do_neg_search:
            # We include the right boundary (-1) if we're searching through to the positives
            this_right_closed = do_pos_search or right_closed
            first_val = minval
            last_val = min(-1, maxval)
            ret += self._subrangeRows(p_enc, first_val, last_val, limit, this_right_closed,
                                      do_count_only)
            if limit is not None:
                limit -= ret if do_count_only else len(ret)
                if limit == 0:
                    return ret

        if do_pos_search:
            first_val = max(0, minval)
            last_val = maxval
            ret += self._subrangeRows(p_enc, first_val, last_val, limit, right_closed,
                                      do_count_only)
        return ret

    def _subrangeRows(self, p_enc, first_val, last_val, limit, right_closed, do_count_only):
        first_key = p_enc + _enc_val_key(first_val)

        am_going_backwards = (first_val < 0)

        last_key = p_enc + _enc_val_key(last_val)

        ret = []
        count = 0

        # Figure out the terminating condition of the loop
        if am_going_backwards:
            term_cmp = bytes.__lt__ if right_closed else bytes.__le__
        else:
            term_cmp = bytes.__gt__ if right_closed else bytes.__ge__

        with self._get_txn() as txn, txn.cursor(self.index_pvt) as cursor:
            if not cursor.set_range(first_key):
                raise DatabaseInconsistent("Missing sentinel")
            if am_going_backwards:
                # set_range sets the cursor at the first key >= first_key, if we're going backwards
                # we actually want the first key <= first_key
                if memToBytes(cursor.key()[:len(first_key)]) > first_key:
                    if not cursor.prev():
                        raise DatabaseInconsistent("Missing sentinel")
                it = cursor.iterprev(keys=True, values=True)
            else:
                it = cursor.iternext(keys=True, values=True)

            for key, value in it:
                if term_cmp(memToBytes(key[:len(last_key)]), last_key):
                    break
                count += 1
                if not do_count_only:
                    ret.append(self._getRowByPkValEnc(txn, value))
                if limit is not None and count >= limit:
                    break
        return count if do_count_only else ret
