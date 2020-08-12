/*
 * polytracker_tracing.h
 *
 *  Created on: Jul 3, 2020
 *      Author: Evan Sultanik
 */
#ifndef POLYTRACKER_INCLUDE_POLYTRACKER_TRACING_H_
#define POLYTRACKER_INCLUDE_POLYTRACKER_TRACING_H_

#include <functional>
#include <set>
#include <string>
#include <string.h>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "dfsan/dfsan_types.h"

namespace polytracker {

extern size_t numTraceEvents;

struct TraceEvent {
  TraceEvent *previous;
  size_t eventIndex;
  TraceEvent();
  virtual ~TraceEvent() = default;
};

struct BasicBlockTrace {
  const char *fname;
  BBIndex index;
  size_t entryCount;

  bool operator==(const BasicBlockTrace &other) const {
    return (fname == other.fname /* these are globals, so it should be fine
                                  * to skip a string compare and just compare
                                  * pointers */
            && index == other.index && entryCount == other.entryCount);
  }

  bool operator<(const BasicBlockTrace &rhs) const {
    auto fnameCmp = strcmp(fname, rhs.fname);
    if (fnameCmp == 0) {
      // these BBs are in the same function
      if (index == rhs.index) {
        // they are the same BB, so compare their entry counter
        return entryCount < rhs.entryCount;
      } else {
        return index < rhs.index;
      }
    } else {
      return fnameCmp < 0;
    }
  }

  std::string str() const;
};

template<typename BB>
struct BasicBlockTraceHasher {
  std::size_t operator()(BB bb) const {
    using std::hash;
    using std::size_t;
    using std::string;

    return (hash<uint64_t>()(bb.index) << 1) ^
           (hash<decltype(BasicBlockTrace::entryCount)>()(bb.entryCount)
            << 1);
  }
};

struct BasicBlockTraceComparator {
  std::size_t operator()(std::reference_wrapper<const BasicBlockTrace> lhs, std::reference_wrapper<const BasicBlockTrace> rhs) const {
    return lhs.get() < rhs.get();
  }
};

class BasicBlockEntry : public TraceEvent {
  mutable size_t entryCounter;

public:
  const char *fname;
  BBIndex index;

  BasicBlockEntry(const char *fname, BBIndex index)
      : entryCounter(0), fname(fname), index(index) {}

  size_t entryCount() const;

  operator BasicBlockTrace() const { return bb(); }

  BasicBlockTrace bb() const {
    return BasicBlockTrace{fname, index, entryCount()};
  }

  std::string str() const { return BasicBlockTrace(*this).str(); }
};

struct FunctionCall : public TraceEvent {
  const char *fname;

  FunctionCall(const char* fname) : fname(fname) {}

  const BasicBlockEntry *getCaller() const {
    for (auto event = previous; event; event = event->previous) {
      if (auto bb = dynamic_cast<const BasicBlockEntry*>(event)) {
        return bb;
      }
    }
    return nullptr;
  }
};

struct FunctionReturn : public TraceEvent {
  const char *fname;
  const BasicBlockEntry *returningTo;

  FunctionReturn(const char *fname, const BasicBlockEntry *returningTo) : fname(fname), returningTo(returningTo) {}
};

class TraceEventStack {
  TraceEvent *head;

public:
  std::vector<const TraceEvent*> eventHistory;

  TraceEventStack() : head(nullptr) {}
  ~TraceEventStack() {
    for (auto event : eventHistory) {
      delete event;
    }
    head = nullptr;
  }
  /* disallow copying to avoid the memory management headache
   * and avoid the runtime overhead of using shared pointers */
  TraceEventStack(const TraceEventStack &) = delete;
  operator bool() const { return head != nullptr; }
  bool empty() const { return head != nullptr; }
  /**
   * This object will assume ownership of the memory pointed to by event.
   */
  void push(TraceEvent *event) {
    event->previous = head;
    head = event;
    eventHistory.push_back(event);
  }
  template <typename T,
            typename std::enable_if<std::is_base_of<TraceEvent, T>::value>::type
                * = nullptr,
            typename... Ts>
  T *emplace(Ts &&... args) {
    auto t = new T(std::forward<Ts>(args)...);
    push(t);
    return t;
  }
  TraceEvent *peek() const { return head; }
  bool pop() {
    if (head) {
      auto oldHead = head;
      head = head->previous;
      return true;
    } else {
      return false;
    }
  }
};

class Trace {
  /* lastUsages maps canonical byte offsets to the last basic block trace
   * in which they were used */
  std::unordered_map<dfsan_label, const BasicBlockEntry *> lastUsages;
  std::unordered_map<const BasicBlockEntry *, std::set<dfsan_label>> lastUsagesByBB;
public:
  std::unordered_map<std::thread::id, TraceEventStack> eventStacks;

  TraceEventStack &getStack(std::thread::id thread) {
    return eventStacks[std::this_thread::get_id()];
  }
  TraceEventStack *currentStack() {
    return &eventStacks[std::this_thread::get_id()];
  }
  const TraceEventStack *currentStack() const {
    auto stackIter = eventStacks.find(std::this_thread::get_id());
    if (stackIter != eventStacks.end()) {
      return &stackIter->second;
    } else {
      return nullptr;
    }
  }
  TraceEvent *lastEvent() const {
    if (auto stack = currentStack()) {
      return stack->peek();
    } else {
      return nullptr;
    }
  }
  TraceEvent *secondToLastEvent() const {
    if (auto last = lastEvent()) {
      return last->previous;
    } else {
      return nullptr;
    }
  }
  /**
   * Returns the current basic block for the calling thread
   */
  const BasicBlockEntry *currentBB() const {
    auto event = lastEvent();
    for (auto event = lastEvent(); event; event = event->previous) {
      if(auto bbe = dynamic_cast<BasicBlockEntry *>(event)) {
        return bbe;
      } else if (dynamic_cast<FunctionCall *>(event)) {
        return nullptr;
      }
    }
    return nullptr;
  }
  void setLastUsage(dfsan_label canonicalByte, const BasicBlockEntry *bb) {
    const auto oldValue = lastUsages.find(canonicalByte);
    if (oldValue != lastUsages.cend()) {
      // We are updating the last usage,
      // so remove the old value from the reverse map
      lastUsagesByBB[oldValue->second].erase(canonicalByte);
    }
    lastUsages[canonicalByte] = bb;
    lastUsagesByBB[bb].insert(canonicalByte);
  }
  const BasicBlockEntry *getLastUsage(dfsan_label label) const {
    auto luIter = lastUsages.find(label);
    if (luIter != lastUsages.end()) {
      return luIter->second;
    } else {
      return nullptr;
    }
  }
  decltype(lastUsages) taints() const {
    return lastUsages;
  }
  const std::set<dfsan_label> taints(const BasicBlockEntry *bb) const {
    const auto ret = lastUsagesByBB.find(bb);
    if (ret == lastUsagesByBB.cend()) {
      return {};
    } else {
      return ret->second;
    }
  }
};

} /* namespace polytracker */

#endif /* POLYTRACKER_INCLUDE_POLYTRACKER_TRACING_H_ */
