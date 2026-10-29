#include "api.hpp"
#include <algorithm>
#include <utility>

std::vector<pager::Action> pager::recover(const Image& input) {
    struct Tx { LSN last=0; int state=0; };
    struct Swap { LSN lsn; std::pair<std::uint32_t,std::uint32_t> a, b; };
    std::map<std::uint32_t,Tx> active;
    std::map<LSN,Record> records;
    std::vector<Swap> swaps;
    auto pages=input.pages;
    std::vector<Action> out;
    LSN last=input.log.empty() ? input.base_lsn : input.log.back().lsn;

    // Analysis: transaction table from the retained log. SWAP records belong
    // to no transaction.
    auto analyze=[&](const Record& r) {
        records[r.lsn]=r;
        last=r.lsn;
        if (r.kind==SWAP) return;
        auto& t=active[r.tx]; t.last=r.lsn;
        if (r.kind==PREPARE || r.kind==COMMIT || r.kind==ABORT) t.state=r.kind;
        if (r.kind==END) active.erase(r.tx);
    };
    for (const auto& r:input.log) {
        analyze(r);
        if (r.kind==SWAP) swaps.push_back({r.lsn,{r.page,r.slot},{r.page2,r.slot2}});
    }

    auto persist=[&](std::uint32_t p) {
        Action a; a.op='P'; a.page_id=p; a.page=pages[p]; out.push_back(a);
    };
    // Redo: bring every page up to the end of the durable log.
    for (const auto& r:input.log) {
        if (r.kind==UPDATE || r.kind==CLR) {
            if (pages[r.page].lsn<r.lsn) {
                pages[r.page].values[r.slot]=r.after; pages[r.page].lsn=r.lsn;
                persist(r.page);
            }
        } else if (r.kind==SWAP) {
            if (pages[r.page].lsn<r.lsn) {
                pages[r.page].values[r.slot]=r.after;
                if (r.page2==r.page) pages[r.page].values[r.slot2]=r.before;
                pages[r.page].lsn=r.lsn;
                persist(r.page);
            }
            if (r.page2!=r.page && pages[r.page2].lsn<r.lsn) {
                pages[r.page2].values[r.slot2]=r.before; pages[r.page2].lsn=r.lsn;
                persist(r.page2);
            }
        }
    }

    // Where the item changed by an UPDATE is now: follow every later SWAP.
    auto where=[&](const Record& u) {
        std::pair<std::uint32_t,std::uint32_t> cell{u.page,u.slot};
        auto it=std::upper_bound(swaps.begin(),swaps.end(),u.lsn,
                                 [](LSN v,const Swap& s){ return v<s.lsn; });
        for (; it!=swaps.end(); ++it) {
            if (cell==it->a) cell=it->b;
            else if (cell==it->b) cell=it->a;
        }
        return cell;
    };

    auto append=[&](Record r) {
        r.lsn=last+1;
        r.prev=active.at(r.tx).last;
        Action a; a.op='A'; a.record=r; out.push_back(a);
        analyze(r);
        Action f; f.op='F'; f.upto=last; out.push_back(f);
        return r;
    };
    std::vector<std::uint32_t> ids;
    for (const auto& item:active) ids.push_back(item.first);
    for (auto id:ids) {
        int state=active.at(id).state;
        if (state==PREPARE) {
            auto decision=input.decisions.find(id);
            if (decision==input.decisions.end()) continue;
            Record r; r.tx=id; r.kind=decision->second==1?COMMIT:ABORT;
            append(r); state=r.kind;
        }
        if (state==COMMIT) {
            Record r; r.tx=id; r.kind=END; append(r); continue;
        }
        if (state!=ABORT) {
            Record r; r.tx=id; r.kind=ABORT; append(r);
        }
        LSN cursor=active.at(id).last;
        while (cursor) {
            auto r=records.at(cursor);
            if (r.kind==CLR || r.kind==NESTED) cursor=r.undo;
            else if (r.kind==UPDATE) {
                auto cell=where(r);
                Record c;
                c.tx=id; c.kind=CLR; c.undo=r.prev; c.page=cell.first;
                c.slot=cell.second; c.after=r.before; c.target=r.lsn;
                c=append(c);
                pages[c.page].values[c.slot]=c.after; pages[c.page].lsn=c.lsn;
                persist(c.page);
                cursor=r.prev;
            } else cursor=r.prev;
        }
        Record r; r.tx=id; r.kind=END; append(r);
    }
    return out;
}
