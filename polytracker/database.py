from enum import IntEnum, IntFlag
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Set, Tuple, Union

from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.orm.session import Session
from sqlalchemy.orm.query import Query
from sqlalchemy import (
    BigInteger,
    BLOB,
    Column,
    create_engine,
    Enum as SQLEnum,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    Text,
    SmallInteger,
    UniqueConstraint,
)

from tqdm import tqdm

from .cache import LRUCache
from .polytracker import ProgramTrace
from .repl import PolyTrackerREPL
from .taint_forest import TaintForest, TaintForestNode
from .tracing import (
    BasicBlock,
    BasicBlockEntry,
    BasicBlockType,
    ByteOffset,
    Function,
    FunctionEntry,
    FunctionInvocation,
    FunctionReturn,
    Input,
    TaintAccess,
    Taints,
    TraceEvent,
)

Base = declarative_base()


class ByteAccessType(IntFlag):
    UNKNOWN_ACCESS = 0
    INPUT_ACCESS = 1
    CMP_ACCESS = 2
    READ_ACCESS = 4


class EventType(IntEnum):
    FUNC_ENTER = 0
    FUNC_RET = 1
    BLOCK_ENTER = 2
    TAINT_ACCESS = 3


class EdgeType(IntEnum):
    FORWARD = 0
    BACKWARD = 1


def stream_results(query: Query, window_size: int = 10000) -> Iterator:
    start = 0
    while True:
        stop = start + window_size
        results = query.slice(start, stop).all()
        if len(results) == 0:
            break
        yield from results
        start += window_size


class DBInput(Base, Input):  # type: ignore
    __tablename__ = "input"
    uid = Column("id", Integer, primary_key=True)
    stored_content = Column("content", BLOB, nullable=True)
    path = Column(Text)
    track_start = Column(BigInteger)
    track_end = Column(BigInteger)
    size = Column(BigInteger)
    trace_level = Column(Integer)

    events = relationship("DBTraceEvent", order_by="asc(DBTraceEvent.event_id)")


class DBFunction(Base, Function):  # type: ignore
    __tablename__ = "func"
    id = Column(Integer, primary_key=True)
    name = Column(Text)

    basic_blocks = relationship("DBBasicBlock")

    incoming_edges: Iterable["FunctionCFGEdge"] = relationship(
        "FunctionCFGEdge", primaryjoin="DBFunction.id==FunctionCFGEdge.dest_id"
    )
    outgoing_edges: Iterable["FunctionCFGEdge"] = relationship(
        "FunctionCFGEdge", primaryjoin="DBFunction.id==FunctionCFGEdge.src_id"
    )
    accessed_labels = relationship(
        "AccessedLabel",
        primaryjoin="and_(DBFunction.id==remote(DBBasicBlock.function_id), "
        "foreign(DBTraceEvent.block_gid)==DBBasicBlock.id, "
        "foreign(AccessedLabel.event_id)==DBTraceEvent.event_id)",
        viewonly=True,
    )

    def taints(self) -> Taints:
        return DBTaintForestNode.taints(
            (label.event.taint_forest_node for label in self.accessed_labels)
        )

    @property
    def function_index(self) -> int:  # type: ignore
        return self.id

    def calls_to(self) -> Set["Function"]:
        return {
            edge.dest
            for edge in self.outgoing_edges
            if edge.dest is not None and edge.edge_type == EdgeType.FORWARD
        }

    def called_from(self) -> Set["Function"]:
        return {
            edge.src
            for edge in self.incoming_edges
            if edge.src is not None and edge.edge_type == EdgeType.FORWARD
        }


class FunctionCFGEdge(Base):  # type: ignore
    __tablename__ = "func_cfg"
    dest_id = Column("dest", Integer, ForeignKey("func.id"))
    src_id = Column("src", Integer, ForeignKey("func.id"))
    input_id = Column(Integer, ForeignKey("input.id"))
    thread_id = Column(Integer)
    event_id = Column(BigInteger, ForeignKey("events.event_id"))
    edge_type = Column(SmallInteger, SQLEnum(EdgeType))

    dest = relationship(
        "DBFunction", foreign_keys=[dest_id], back_populates="incoming_edges"
    )
    src = relationship(
        "DBFunction", foreign_keys=[src_id], back_populates="outgoing_edges"
    )
    source = relationship("DBInput")
    event = relationship("DBTraceEvent")

    __table_args__ = (PrimaryKeyConstraint("input_id", "dest", "src"),)


class DBBasicBlock(Base, BasicBlock):  # type: ignore
    __tablename__ = "basic_block"
    id = Column(BigInteger, primary_key=True)
    # function_id should always be equal to (id >> 32), but we have it for convenience
    function_id = Column(BigInteger, ForeignKey("func.id"))
    attributes = Column("block_attributes", Integer, SQLEnum(BasicBlockType))

    __table_args__ = (UniqueConstraint("id", "block_attributes"),)

    events: Iterable["DBTraceEvent"] = relationship(
        "DBTraceEvent", order_by="asc(DBTraceEvent.event_id)"
    )
    accessed_labels = relationship(
        "AccessedLabel",
        primaryjoin="and_(DBBasicBlock.id==remote(DBTraceEvent.block_gid), "
        "foreign(AccessedLabel.event_id)==DBTraceEvent.event_id)",
        viewonly=True,
    )
    function = relationship("DBFunction", back_populates="basic_blocks")

    _children: Optional[Set[BasicBlock]] = None
    _predecessors: Optional[Set[BasicBlock]] = None

    @property
    def index_in_function(self) -> int:  # type: ignore
        return self.id & 0x0000FFFF

    def taints(self) -> Taints:
        return DBTaintForestNode.taints(
            (label.taint_forest_node for label in self.accessed_labels)
        )

    def _discover_neighbors(self):
        if self._children is not None and self._predecessors is not None:
            return
        self._children = set()
        self._predecessors = set()
        next_event_queue = []
        prev_event_queue = []
        with tqdm(
            desc=f"resolving neighborhood for event {self.id}",
            unit=" BBs",
            total=3,
            leave=False,
        ) as t:
            t.update(1)
            for event in tqdm(
                self.events, desc="processing", unit=" events", leave=False
            ):
                next_event = event.next_event
                if next_event is not None:
                    next_event_queue.append(next_event)
                prev_event = event.previous_event
                if prev_event is not None:
                    prev_event_queue.append(prev_event)
            t.update(1)
            with tqdm(
                desc="processing",
                unit="descendants",
                leave=False,
                total=len(next_event_queue),
            ) as d:
                while next_event_queue:
                    d.update(1)
                    next_event = next_event_queue.pop()
                    if isinstance(next_event, BasicBlockEntry):
                        if next_event.basic_block != self:
                            self._children.add(next_event.basic_block)
                    else:
                        grandchild = next_event.next_event
                        if grandchild is not None:
                            next_event_queue.append(grandchild)
                            d.total += 1
            t.update(1)
            with tqdm(
                desc="processing",
                unit="predecessors",
                leave=False,
                total=len(prev_event_queue),
            ) as d:
                while prev_event_queue:
                    d.update(1)
                    prev_event = prev_event_queue.pop()
                    if isinstance(prev_event, BasicBlockEntry):
                        if prev_event.basic_block != self:
                            self._predecessors.add(prev_event.basic_block)
                    else:
                        grandparent = prev_event.previous_event
                        if grandparent is not None:
                            prev_event_queue.append(grandparent)
                            d.total += 1

    @property
    def predecessors(self) -> Set[BasicBlock]:  # type: ignore
        self._discover_neighbors()
        return self._predecessors  # type: ignore

    @property
    def children(self) -> Set[BasicBlock]:  # type: ignore
        self._discover_neighbors()
        return self._children  # type: ignore


class AccessedLabel(Base):  # type: ignore
    __tablename__ = "accessed_label"
    event_id = Column(BigInteger, ForeignKey("events.event_id"))
    label = Column(Integer, ForeignKey("taint_forest.label"))
    access_type = Column(SmallInteger, SQLEnum(ByteAccessType))

    event = relationship("DBTaintAccess", uselist=False)

    __table_args__ = (PrimaryKeyConstraint("event_id", "label", "access_type"),)

    def __lt__(self, other):
        return hasattr(other, "label") and self.label < other.label

    def taints(self) -> Taints:
        return DBTaintForestNode.taints((self.taint_forest_node,))


class DBTraceEvent(Base):  # type: ignore
    __tablename__ = "events"
    event_id = Column(BigInteger)  # globally unique, globally sequential event counter
    thread_event_id = Column(
        BigInteger
    )  # unique-to-thread id, sequential within the thread
    event_type = Column(SmallInteger, SQLEnum(EventType))
    input_id = Column(Integer, ForeignKey("input.id"))
    thread_id = Column(Integer)
    block_gid = Column(BigInteger, ForeignKey("basic_block.id"))
    func_event_id = Column(BigInteger, ForeignKey("events.event_id"))

    __table_args__ = (PrimaryKeyConstraint("input_id", "event_id"),)

    input = relationship("DBInput", back_populates="events")
    basic_block = relationship("DBBasicBlock", back_populates="events")
    accessed_labels = relationship("AccessedLabel")

    __mapper_args__ = {"polymorphic_on": "event_type"}

    _queried_for_entry: bool = False
    _function_entry: Optional[FunctionEntry] = None
    trace: Optional["DBProgramTrace"] = None

    @property
    def uid(self) -> int:  # type: ignore
        return self.event_id

    @property
    def function_entry(self) -> Optional[FunctionEntry]:
        if not self._queried_for_entry:
            self._queried_for_entry = True
            if self.trace is not None:
                try:
                    entry = self.trace.get_event(self.func_event_id)
                    if isinstance(entry, FunctionEntry):
                        self._function_entry = entry
                    else:
                        self._function_entry = None
                except KeyError:
                    self._function_entry = None
            else:
                try:
                    self._function_entry = (
                        Session.object_session(self)
                        .query(DBFunctionEntry)
                        .filter(DBFunctionEntry.event_id == self.func_event_id)
                        .one()
                    )
                except NoResultFound:
                    self._function_entry = None
        return self._function_entry

    @property
    def next_event(self) -> Optional["TraceEvent"]:
        if self.trace is not None:
            # Use the LRU event cache in the trace:
            try:
                return self.trace.get_thread_event(thread_event_id=self.thread_event_id + 1, thread_id=self.thread_id)
            except KeyError:
                return None
        elif not hasattr(self, "_next_event"):
            session = Session.object_session(self)
            try:
                setattr(
                    self,
                    "_next_event",
                    session.query(DBTraceEvent)
                    .filter(
                        DBTraceEvent.thread_event_id == self.thread_event_id + 1,
                        DBTraceEvent.thread_id == self.thread_id,
                    )
                    .one()
                )
                setattr(self._next_event, "_prev_event", self)
            except NoResultFound:
                setattr(self, "_next_event", None)
        return self._next_event

    @property
    def previous_event(self) -> Optional["TraceEvent"]:
        if self.trace is not None:
            # Use the LRU event cache in the trace:
            try:
                return self.trace.get_thread_event(thread_event_id=self.thread_event_id - 1, thread_id=self.thread_id)
            except KeyError:
                return None
        elif not hasattr(self, "_prev_event"):
            session = Session.object_session(self)
            try:
                setattr(
                    self,
                    "_prev_event",
                    session.query(DBTraceEvent)
                    .filter(
                        DBTraceEvent.thread_event_id == self.thread_event_id - 1,
                        DBTraceEvent.thread_id == self.thread_id,
                    )
                    .one()
                )
                setattr(self._prev_event, "_next_event", self)
            except NoResultFound:
                setattr(self, "_prev_event", None)
        return self._prev_event

    @property
    def next_global_event(self) -> Optional["TraceEvent"]:
        if self.trace is not None:
            # Use the LRU event cache in the trace:
            try:
                return self.trace.get_event(self.event_id + 1)
            except KeyError:
                return None
        session = Session.object_session(self)
        try:
            return (
                session.query(DBTraceEvent)
                .filter(DBTraceEvent.event_id == self.event_id + 1)
                .one()
            )
        except NoResultFound:
            return None

    @property
    def previous_global_event(self) -> Optional["TraceEvent"]:
        if self.trace is not None:
            # Use the LRU event cache in the trace:
            try:
                return self.trace.get_event(self.event_id - 1)
            except KeyError:
                return None
        session = Session.object_session(self)
        try:
            return (
                session.query(DBTraceEvent)
                .filter(DBTraceEvent.event_id == self.event_id - 1)
                .one()
            )
        except NoResultFound:
            return None

    def taints(self) -> Taints:
        return Taints(())


class DBTaintAccess(DBTraceEvent, TaintAccess):  # type: ignore
    __mapper_args__ = {
        "polymorphic_identity": EventType.TAINT_ACCESS,  # type: ignore
    }

    accessed_label = relationship(
        "AccessedLabel",
        primaryjoin="AccessedLabel.event_id==DBTaintAccess.event_id",
        uselist=False,
    )
    taint_forest_node = relationship(
        "DBTaintForestNode",
        primaryjoin="and_(DBTaintAccess.event_id==remote(AccessedLabel.event_id), "
        "AccessedLabel.label==foreign(DBTaintForestNode.label), "
        "DBTaintForestNode.input_id==DBTaintAccess.input_id)",
        viewonly=True,
        uselist=False,
    )

    def taints(self) -> Taints:
        return DBTaintForestNode.taints((self.taint_forest_node,))


class DBBasicBlockEntry(DBTraceEvent, BasicBlockEntry):  # type: ignore
    __mapper_args__ = {
        "polymorphic_identity": EventType.BLOCK_ENTER,  # type: ignore
    }

    @property
    def bb_index(self) -> int:
        return self.block_gid & 0x0000FFFF

    @property
    def function_index(self) -> int:
        return (self.func_gid >> 32) & 0xFFFF

    def taint_accesses(self) -> Iterator[DBTaintAccess]:
        next_event = self.next_event
        while isinstance(next_event, DBTaintAccess):
            yield next_event
            next_event = next_event.next_event

    def taints(self) -> Taints:
        return DBTaintForestNode.taints(
            (access.taint_forest_node for access in self.taint_accesses())
        )


class DBFunctionEntry(DBTraceEvent, FunctionEntry):  # type: ignore
    __mapper_args__ = {"polymorphic_identity": EventType.FUNC_ENTER}  # type: ignore

    @property
    def function_return(self) -> Optional["FunctionReturn"]:
        try:
            return (
                Session.object_session(self)
                .query(DBFunctionReturn)
                .filter(DBFunctionReturn.func_event_id == self.uid)
                .one()
            )
        except NoResultFound:
            return None


class DBFunctionReturn(DBTraceEvent, FunctionReturn):  # type: ignore
    __mapper_args__ = {"polymorphic_identity": EventType.FUNC_RET}  # type: ignore


class DBFunctionInvocation(FunctionInvocation):
    def __init__(self, function_entry: DBFunctionEntry, trace: "DBProgramTrace"):
        super().__init__(function_entry)
        self.trace: DBProgramTrace = trace
        self._called_by: Optional[FunctionInvocation] = None

    @property
    def function_entry(self) -> DBFunctionEntry:
        return super().function_entry  # type: ignore

    @property
    def function_return(self) -> Optional[DBFunctionReturn]:
        return super().function_return  # type: ignore

    def called_by(self) -> Optional[FunctionInvocation]:
        if self._called_by is None:
            caller = self.function_entry.caller
            if caller is not None and caller.function_entry is not None:
                self._called_by = DBFunctionInvocation(caller.function_entry, self.trace)  # type: ignore
        return self._called_by

    @property
    def touched_taint(self) -> bool:
        query = self.trace.session.query(AccessedLabel).join(DBTraceEvent).filter(
            DBTraceEvent.thread_id == self.function_entry.thread_id,
            DBTraceEvent.thread_event_id > self.function_entry.thread_event_id
        )
        ret = self.function_return
        if ret is not None:
            query = query.filter(DBTraceEvent.thread_event_id < ret.thread_event_id)
        try:
            return query.limit(1).count() > 0
        except NoResultFound:
            return False

    def taint_accesses(self) -> Iterator[DBTaintAccess]:
        query = self.trace.session.query(DBTaintAccess).join(AccessedLabel).filter(
            DBTaintAccess.thread_id == self.function_entry.thread_id,
            DBTaintAccess.thread_event_id > self.function_entry.thread_event_id
        )
        ret = self.function_return
        if ret is not None:
            query = query.filter(DBTaintAccess.thread_event_id < ret.thread_event_id)
        return stream_results(query.order_by(DBTaintAccess.event_id.asc()))

    def taints(self) -> Taints:
        return DBTaintForestNode.taints(
            (access.taint_forest_node for access in self.taint_accesses())
        )

    def calls(self) -> Iterator["DBFunctionInvocation"]:
        event = self.function_return
        thread_id = self.function_entry.thread_id
        # are we the entrypoint (i.e., main)? If so, we probably don't have a function return, so use the last event:
        if event is None and self == self.trace.entrypoint:
            try:
                event = self.trace.session.query(DBTraceEvent).filter(
                    DBTraceEvent.thread_event_id > self.function_entry.thread_event_id,
                    DBTraceEvent.thread_id == thread_id
                ).order_by(DBTraceEvent.thread_event_id.desc()).limit(1).one()
            except NoResultFound:
                return iter(())
        # work backwards from our function_return, since returns have back-pointers to their associated function_entry:
        ret: List[DBFunctionInvocation] = []
        while event is not None:
            # find the last function return before event:
            try:
                func_return = self.trace.session.query(DBFunctionReturn).filter(
                    DBFunctionReturn.thread_event_id < event.thread_event_id,
                    DBFunctionReturn.thread_event_id > self.function_entry.thread_event_id,
                    DBFunctionReturn.thread_id == thread_id
                ).order_by(DBFunctionReturn.thread_event_id.desc()).limit(1).one()
                event = func_return.function_entry
                if event is not None:
                    ret.append(DBFunctionInvocation(event, self.trace))
            except NoResultFound:
                # there are no more function returns in this invocation
                break
        return reversed(ret)


class DBProgramTrace(ProgramTrace):
    def __init__(self, session: Session, event_cache_size: Optional[int] = 15000000):
        self.session: Session = session
        self.event_cache: LRUCache[int, TraceEvent] = LRUCache(max_size=event_cache_size)
        self.thread_event_cache: LRUCache[Tuple[int, int], DBTraceEvent] = LRUCache(max_size=event_cache_size)

    @staticmethod
    @PolyTrackerREPL.register("load_trace")
    def load(db_path: Union[str, Path], read_only: bool = True) -> "DBProgramTrace":
        """loads a trace from the database emitted by an instrumented binary"""
        engine = create_engine(f"sqlite:///{db_path!s}")
        if read_only:
            session_maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        else:
            session_maker = sessionmaker(bind=engine)
        session = session_maker()
        if read_only:
            def abort_read_only(*_, **__):
                raise ValueError(f"Database {db_path} was loaded as read only! To write to the database, make sure "
                                 "PolyTrackerTrace.load is called with the `read_only` argument set to True.")
            session.flush = abort_read_only
        db = DBProgramTrace(session)
        if db_path != ":memory:" and sum(1 for _ in db.inputs) > 1:
            raise ValueError(f"{db_path} contains traces from multiple inputs.\nIt is likely the case that the same "
                             "database was reused for more than one run of the instrumented binary.\nThis feature is "
                             "not yet fully implemented.\nPlease track this GitHub issue for further details and "
                             "progress:\n    https://github.com/trailofbits/polytracker/issues/6353\nIn the mean time, "
                             "you should use a separate database for every instrumented run of a binary.")
        return db

    def __len__(self) -> int:
        return self.session.query(DBTraceEvent).count()

    def __iter__(self) -> Iterator[TraceEvent]:
        for event in stream_results(
            self.session.query(DBTraceEvent).order_by(DBTraceEvent.event_id.asc())
        ):
            event.trace = self
            self.event_cache[event.event_id] = event
            self.thread_event_cache[(event.thread_id, event.thread_event_id)] = event
            yield event

    def function_trace(self) -> Iterator[DBFunctionEntry]:
        for function_entry in stream_results(
            self.session.query(DBFunctionEntry).order_by(DBFunctionEntry.event_id.asc())
        ):
            function_entry.trace = self
            self.event_cache[function_entry.event_id] = function_entry
            self.thread_event_cache[(function_entry.thread_id, function_entry.thread_event_id)] = function_entry
            yield function_entry

    def next_function_entry(self, after: Optional[FunctionEntry] = None) -> Optional[FunctionEntry]:
        try:
            if after is None:
                return self.session.query(DBFunctionEntry).order_by(DBFunctionEntry.event_id.asc()).limit(1).one()
            elif isinstance(after, DBFunctionEntry):
                return self.session.query(DBFunctionEntry).filter(
                    DBFunctionEntry.thread_event_id > after.thread_event_id,
                    DBFunctionEntry.thread_id == after.thread_id
                ).order_by(DBFunctionEntry.thread_event_id.asc()).limit(1).one()
            else:
                return super().next_function_entry(after)
        except NoResultFound:
            return None

    @property
    def entrypoint(self) -> Optional[DBFunctionInvocation]:
        try:
            return DBFunctionInvocation(next(iter(self.function_trace())), trace=self)
        except StopIteration:
            return None

    def has_event(self, uid: int) -> bool:
        return uid in self.event_cache

    def get_event(self, uid: int) -> TraceEvent:
        try:
            return self.event_cache[uid]
        except KeyError:
            pass
        try:
            event = self.session.query(DBTraceEvent).filter(DBTraceEvent.event_id == uid).limit(1).one()
            event.trace = self
            self.event_cache[uid] = event
            return event
        except NoResultFound:
            pass
        raise KeyError(uid)

    def has_thread_event(self, thread_event_id: int, thread_id: int) -> bool:
        return (thread_id, thread_event_id) in self.thread_event_cache

    def get_thread_event(self, thread_event_id: int, thread_id: int) -> TraceEvent:
        try:
            return self.thread_event_cache[(thread_id, thread_event_id)]
        except KeyError:
            pass
        try:
            event = self.session.query(DBTraceEvent).filter(
                DBTraceEvent.thread_event_id == thread_event_id,
                DBTraceEvent.thread_id == thread_id
            ).limit(1).one()
            event.trace = self
            self.thread_event_cache[(thread_id, thread_event_id)] = event
            return event
        except NoResultFound:
            pass
        raise KeyError((thread_event_id, thread_id))

    @property
    def taint_forest(self) -> TaintForest:
        return DBTaintForest(self)

    @property
    def functions(self) -> Iterable[Function]:
        return self.session.query(DBFunction).all()

    def get_function(self, name: str) -> Function:
        try:
            return (
                self.session.query(DBFunction).filter(DBFunction.name.like(name)).one()
            )
        except NoResultFound:
            pass
        raise KeyError(name)

    def has_function(self, name: str) -> bool:
        return self.session.query(DBFunction).filter(DBFunction.name.like(name)).limit(1).count() > 0

    @property
    def basic_blocks(self) -> Iterable[BasicBlock]:
        return self.session.query(DBBasicBlock).all()

    def access_sequence(self) -> Iterator[TaintAccess]:
        yield from stream_results(
            self.session.query(DBTaintAccess).order_by(DBTaintAccess.event_id.asc())
        )

    @property
    def num_accesses(self) -> int:
        return self.session.query(DBTaintAccess).count()

    @property
    def inputs(self) -> Iterable[Input]:
        return self.session.query(DBInput)

    def __getitem__(self, uid: int) -> TraceEvent:
        raise NotImplementedError()

    def __contains__(self, uid: int):
        raise NotImplementedError()


class PolytrackerItem(Base):  # type: ignore
    __tablename__ = "polytracker"
    store_key = Column(Text)
    value = Column(Text)
    __table_args__ = (PrimaryKeyConstraint("store_key", "value"),)


class CanonicalMap(Base):  # type: ignore
    __tablename__ = "canonical_map"
    input_id = Column(Integer, ForeignKey("input.id"))
    taint_label = Column(BigInteger)
    file_offset = Column(BigInteger)

    __table_args__ = (PrimaryKeyConstraint("input_id", "taint_label", "file_offset"),)

    source = relationship("DBInput")


class TaintedChunksItem(Base):  # type: ignore
    __tablename__ = "tainted_chunks"
    input_id = Column(Integer, ForeignKey("input.id"))
    start_offset = Column(BigInteger)
    end_offset = Column(BigInteger)
    __table_args__ = (PrimaryKeyConstraint("input_id", "start_offset", "end_offset"),)


class DBTaintForestNode(Base, TaintForestNode):  # type: ignore
    __tablename__ = "taint_forest"
    parent_one_id = Column("parent_one", Integer, ForeignKey("taint_forest.label"))
    parent_two_id = Column("parent_two", Integer, ForeignKey("taint_forest.label"))
    label = Column(Integer, ForeignKey("accessed_label.label"))
    input_id = Column(Integer, ForeignKey("input.id"))

    __table_args__ = (PrimaryKeyConstraint("input_id", "label"),)

    source = relationship("DBInput")
    accessed_labels = relationship("AccessedLabel", foreign_keys=[label, input_id])

    def __hash__(self):
        return hash((self.input_id, self.label))

    def __eq__(self, other):
        return (
            isinstance(other, DBTaintForestNode)
            and self.input_id == other.input_id
            and self.label == other.label
        )

    def __lt__(self, other):
        return isinstance(other, DBTaintForestNode) and self.label < other.label

    def __str__(self):
        return f"I{self.input_id}L{self.label}"

    def is_canonical(self) -> bool:
        assert (self.parent_one_id > 0 and self.parent_two_id > 0) or (self.parent_one_id == self.parent_two_id == 0)
        return self.parent_one_id == 0

    @property
    def parent_one(self) -> Optional["DBTaintForestNode"]:
        if not hasattr(self, "_parent_one"):
            try:
                setattr(
                    self,
                    "_parent_one",
                    Session.object_session(self).query(DBTaintForestNode).filter(
                        DBTaintForestNode.label == self.parent_one_id,
                        DBTaintForestNode.input_id == self.input_id
                    ).one()
                )
            except NoResultFound:
                setattr(self, "_parent_one", None)
        return self._parent_one

    @property
    def parent_two(self) -> Optional["DBTaintForestNode"]:
        if not hasattr(self, "_parent_two"):
            try:
                setattr(
                    self,
                    "_parent_two",
                    Session.object_session(self).query(DBTaintForestNode).filter(
                        DBTaintForestNode.label == self.parent_two_id,
                        DBTaintForestNode.input_id == self.input_id
                    ).one()
                )
            except NoResultFound:
                setattr(self, "_parent_two", None)
        return self._parent_two

    @staticmethod
    def taints(labels: Iterable["DBTaintForestNode"]) -> Taints:
        # reverse the labels to reduce the likelihood of reproducing work
        history: Set[DBTaintForestNode] = set(labels)
        node_stack: List[DBTaintForestNode] = sorted(list(set(history)), reverse=True)
        taints: Set[ByteOffset] = set()
        if len(node_stack) < 10:
            labels_str = ", ".join(map(str, node_stack))
        else:
            labels_str = f"{len(node_stack)} labels"
        session: Optional[Session] = None
        with tqdm(
            desc=f"finding canonical taints for {labels_str}",
            leave=False,
            delay=5.0,
            bar_format="{l_bar}{bar}| [{elapsed}<{remaining}, {rate_fmt}{postfix}]'",
            total=sum(node.label for node in node_stack),
        ) as t:
            while node_stack:
                node = node_stack.pop()
                t.update(node.label)
                if node.parent_one_id == 0:
                    assert node.parent_two_id == 0
                    if session is None:
                        session = Session.object_session(node)
                    try:
                        file_offset = (
                            session.query(CanonicalMap)
                            .filter(
                                CanonicalMap.taint_label == node.label,
                                CanonicalMap.input_id == node.input_id,
                            )
                            .one()
                            .file_offset
                        )
                    except NoResultFound:
                        raise ValueError(
                            f"Taint label {node.label} is not in the canonical mapping!"
                        )
                    taints.add(ByteOffset(source=node.source, offset=file_offset))
                else:
                    parent1, parent2 = node.parent_one, node.parent_two
                    assert parent1 is not None and parent2 is not None
                    if parent1 not in history:
                        history.add(parent1)
                        node_stack.append(parent1)
                        t.total += parent1.label
                    if parent2 not in history:
                        history.add(parent2)
                        node_stack.append(parent2)
                        t.total += parent2.label
        return Taints(taints)


class DBTaintForest(TaintForest):
    def __init__(self, trace: DBProgramTrace):
        self.trace: DBProgramTrace = trace

    def nodes(self) -> Iterator[TaintForestNode]:
        yield from stream_results(self.trace.session.query(DBTaintForestNode).order_by(DBTaintForestNode.label.desc()))

    def __len__(self):
        return self.trace.session.query(DBTaintForestNode).count()
