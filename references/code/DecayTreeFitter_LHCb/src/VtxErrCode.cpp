#include "VtxErrCode.h"


namespace DecayTreeFitter
{
  std::ostream& operator<<(std::ostream& os, const ErrCode& ec) 
  {
    unsigned int flag = ec.flag() ; 
    os << "flag=" << flag << " " ;
    if(flag) {
      if(flag&ErrCode::pocafailure) os << "pocafailure " ;
      if(flag&ErrCode::baddistance) os << "baddistance " ;
      if(flag&ErrCode::inversionerror) os << "inversionerror " ;
      if(flag&ErrCode::badsetup) os << "badsetup " ;
      if(flag&ErrCode::divergingconstraint) os << "divergingconstraint " ;
      if(flag&ErrCode::slowdivergingfit) os << "slowdivergingfit " ;
      if(flag&ErrCode::fastdivergingfit) os << "fastdivergingfit " ;
    } else {
      os << "success " ;
    }
    return os ;
  }
}
