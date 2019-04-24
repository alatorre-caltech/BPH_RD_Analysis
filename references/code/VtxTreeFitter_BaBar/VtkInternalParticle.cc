#include "BaBar/BaBar.hh"
#include <iomanip>
#include <algorithm>
#include <Beta/BtaCandidate.hh>
#include <BetaCoreTools/BtaPrintTree.hh>
#include <ErrLogger/ErrLog.hh>
#include <TrkBase/TrkFit.hh>
#include <TrkBase/TrkRecoTrk.hh>
#include <TrkBase/TrkDifTraj.hh>
#include <TrkBase/TrkPoca.hh>
#include <TrkBase/TrkExchangePar.hh>
#include <TrajGeom/TrkLineTraj.hh>

#include "VtxTreeFitter/VtkInternalParticle.hh"
#include "VtxTreeFitter/VtkMissingParticle.hh"
#include "VtxTreeFitter/VtkFitParams.hh"
#include "VtxTreeFitter/VtkRecoTrack.hh"
#include "VtxTreeFitter/VtkHelixUtils.hh"
using std::cout;
using std::endl;

namespace vtxtreefit
{

  extern int vtxverbose ;

  inline bool 
  sortByType(const ParticleBase* lhs, const ParticleBase* rhs) 
  {
    int lhstype = lhs->type() ;
    int rhstype = rhs->type() ;
    bool rc = false ;
    if( lhstype == rhstype  &&
	lhstype == ParticleBase::kRecoTrack )
      rc =  lhs->bc()->pt() > rhs->bc()->pt() ;
    else if( lhs->bc() && rhs->bc() && lhs->bc()->nDaughters()>0 &&
	     rhs->bc()->nDaughters()>0 )
      rc = lhs->nFinalChargedCandidates() >
	rhs->nFinalChargedCandidates() ;
    else
      rc = lhstype < rhstype ;
    return rc ;
  }

  InternalParticle::InternalParticle(const BtaCandidate* bc, const ParticleBase* mother, 
				     bool forceFitAll)
    : ParticleBase(bc,mother),_massconstraint(false),_lifetimeconstraint(false)
  {
    if(bc) {
      HepAListIterator<BtaCandidate> iter=bc->daughterIterator();
      BtaCandidate* daughter ;
      while( (daughter=iter()) )
	addDaughter(daughter,forceFitAll) ;
    }

    // sort the daughters
    std::sort(_daughters.begin(),_daughters.end(),sortByType) ;

    // copy constraints
    _massconstraint     = bc && bc->constraint(BtaConstraint::Mass) ;
    _lifetimeconstraint = bc && bc->constraint(BtaConstraint::Life) ;
    _isconversion = _massconstraint && _daughters.size()==2 && bc && bc->pdtEntry()->lundId()==PdtLund::gamma ;
  }

  InternalParticle::~InternalParticle() 
  {
   for(daucontainer::iterator it = _daughters.begin() ;
       it != _daughters.end() ; ++it)
     delete *it ;
   _daughters.clear() ;
  }

  void InternalParticle::updateIndex(int& offset)
  {
    for(daucontainer::const_iterator it = _daughters.begin() ;
	it != _daughters.end() ; ++it)
      (*it)->updateIndex(offset) ;
    ParticleBase::updateIndex(offset) ;
  }

  ParticleBase* InternalParticle::addDaughter(const BtaCandidate* cand, bool forceFitAll) 
  {
    _daughters.push_back( ParticleBase::createParticle(cand,this,forceFitAll) ) ;
    return _daughters.back() ;
  }

  void InternalParticle::removeDaughter(const ParticleBase* pb) 
  {
    daucontainer::iterator it = std::find(_daughters.begin(),_daughters.end(),pb) ;
    if(it != _daughters.end() ) {
      delete *it ;
      _daughters.erase(it) ;
    } else {
      cout << "ERROR: cannot remove particle, because not found ..." << endl ;
    }
  }

  void InternalParticle::retrieveIndexMap(indexmap& anindexmap) const 
  {
    for(daucontainer::const_iterator it = _daughters.begin() ;
	it != _daughters.end() ; ++it)
      (*it)->retrieveIndexMap(anindexmap) ;
    ParticleBase::retrieveIndexMap(anindexmap)  ;
  }

  int InternalParticle::nFinalChargedCandidates() const {
    int rc=0;
    for(daucontainer::const_iterator it = _daughters.begin() ;
	it != _daughters.end() ; ++it)
      rc +=(*it)->nFinalChargedCandidates() ;
    return rc ;
  }

  void InternalParticle::addToDaughterList(daucontainer& list)
  {
    int posindex = posIndex() ;
    for(daucontainer::iterator it = begin(); it!= end(); ++it) {
      list.push_back(*it) ;
      if( (*it)->posIndex()==posindex ) (*it)->addToDaughterList(list) ;
    }
  }

  bool compTrkTransverseMomentum(const RecoTrack* lhs, const RecoTrack* rhs)
  {
    return lhs->bc()->pt() > rhs->bc()->pt() ;
  }
  
  ErrCode
  InternalParticle::initPar1(FitParams* fitparams)
  {
    // This is the most complicated part of the vertexer: an
    // initialization that always works.

    // There are two ways out: If the BtaCandidate was vertexed
    // before, we can rely on the existing vertex (case A). If not, we
    // need to estimate the vertex position from the daughters; that
    // is very complicated (case B). The momentum is always
    // initialized from the sum of the daughter four-vectors. In the
    // end, it doesn't really matter.

    // FIX ME: Currently, this scheme does not work for B->K*D0, with
    // D0->pi0Ks, because the D0 is initialized before there is a B
    // vertex.
    
    if(vtxverbose>=3)
      cout << "InternalParticle::initPar: " 
	   << bc()->pdtEntry()->name() << " " << _daughters.size() << " "
	   << hasPosition() << " " << posIndex() << endl ;
    
    ErrCode status ;
    int posindex = posIndex() ;

    // logic check: we do not want to call this routine for resonances.
    assert( hasPosition() ) ;
      
    // Start with origin
    for(int row=1; row<=3; ++row) fitparams->par()(row+posindex) = 0 ;
    
    // Step 1: pre-initialization of all daughters
    for(daucontainer::const_iterator it = begin() ; it != end() ; ++it) 
      status |= (*it)->initPar1(fitparams) ;

    // Step 2: initialize the vertex. if we are lucky, we had a
    // 'btaresonant daughter, and we are already done.
    if( fitparams->par()(posindex+1)==0 && fitparams->par()(posindex+2)==0 &&
	fitparams->par()(posindex+3)==0 ) {

      const BtaAbsVertex* vtx(0) ;
      if( bc() && (vtx = bc()->decayVtx()) && 
	  vtx->type()   == BtaAbsVertex::Geometric &&
	  vtx->status() == BtaAbsVertex::Success ) {
	// we found an existing valid vertex. that's fine as well ...
	if(vtxverbose>=2)
	  cout << "using existing vertex: " << vtx->point() << endl ;
	
	HepPoint point = vtx->point() ;
	//     cout << "point: " << point << endl ;
	fitparams->par(posindex+1) = point.x() ;
	fitparams->par(posindex+2) = point.y() ;
	fitparams->par(posindex+3) = point.z() ;
	
      } else {
	// Case B: the hard way ... use the daughters to estimate the
	// vertex. First we check if there are sufficient tracks
	// attached to this vertex. If so, estimate the poca of the
	// two tracks with the highest momentum. This will work for
	// the majority of the cases. If there are not sufficient
	// tracks, add the composites and take the two with the best
	// doca.
	
	// create a vector with all daughters that constitute a
	// 'trajectory' (ie tracks, composites and daughters of
	// resonances.)
	
	daucontainer alldaughters ;
	addToDaughterList( alldaughters ) ;

	// select daughters that are either charged, or have an initialized vertex
	daucontainer vtxdaughters ;
	vector<RecoTrack*> trkdaughters ;
	for(daucontainer::const_iterator it = alldaughters.begin() ;
	    it != alldaughters.end() ; ++it) {
	  if( (*it)->type()==ParticleBase::kRecoTrack ) {
	    trkdaughters.push_back( static_cast<RecoTrack*>(*it) )  ;
	  } else if( (*it)->hasPosition() && fitparams->par((*it)->posIndex()+1)!=0 ) {
	    vtxdaughters.push_back( *it ) ;
	  }
	}
	
	if( trkdaughters.size() >=2 ) {
	  // sort in pT. not very efficient, but it works.
	  if( trkdaughters.size()>2 )
	    std::sort(trkdaughters.begin(),trkdaughters.end(),compTrkTransverseMomentum) ;
	  // now, just take the first two ...
	  RecoTrack* dau1 = trkdaughters[0] ;
	  RecoTrack* dau2 = trkdaughters[1] ;
	  HepVector helix1 = dau1->trkFit()->helix(0).params() ;
	  HepVector helix2 = dau2->trkFit()->helix(0).params() ;
	  double flt1(0),flt2(0) ;
	  HepPoint v ;
	  //double helixdoca = 
	  VtkHelixUtils::helixPoca(helix1,helix2,flt1,flt2,v,_isconversion) ;
	  fitparams->par()(posindex+1) = v.x() ;
	  fitparams->par()(posindex+2) = v.y() ;
	  fitparams->par()(posindex+3) = v.z() ;
	  dau1->setFlightLength( flt1 ) ;
	  dau2->setFlightLength( flt2 ) ;
	  
	} else if(trkdaughters.size()+vtxdaughters.size()>=2)  {
	    
	    // that's unfortunate: no enough charged tracks from this
	    // vertex. need all daughters. create trajectories and use
	    // normal TrkPoca.

	    std::vector<const Trajectory*> trajectories ;
	    for(vector<RecoTrack*>::const_iterator it = trkdaughters.begin() ;
		it != trkdaughters.end() ; ++it)
	      trajectories.push_back(&((*it)->bc()->trkAbsFit()->traj())) ;

	    std::vector<TrkLineTraj> linetrajectories ; // store trajectories of composites
	    linetrajectories.reserve(  vtxdaughters.size() ) ;
	    for(daucontainer::const_iterator it = vtxdaughters.begin() ;
		it != vtxdaughters.end() ; ++it) {
	      //cout << (*it)->bc()->pdtEntry()->name() << endl ;
	      int dauposindex = (*it)->posIndex() ;
	      int daumomindex = (*it)->momIndex() ;
	      HepPoint point(fitparams->par()(dauposindex+1),
			     fitparams->par()(dauposindex+2),
			     fitparams->par()(dauposindex+3)) ;
	      Hep3Vector direction(fitparams->par()(daumomindex+1),
				   fitparams->par()(daumomindex+2),
				   fitparams->par()(daumomindex+3)) ;
	      linetrajectories.push_back(TrkLineTraj(point,direction,1) ) ;
	      trajectories.push_back(&(linetrajectories.back())) ;
	      //daupoint = point ;
	    }
	
	    // we select the two trajectories with the best poca
	    double docabest(99999);
	    TrkErrCode pocastatus ;
	    for( std::vector<const Trajectory*>::iterator it1 = trajectories.begin() ;
		 it1 != trajectories.end(); ++it1 )
	      for( std::vector<const Trajectory*>::iterator it2 = trajectories.begin() ;
		   it2 != it1; ++it2 ) {
		TrkPoca poca(**it1,0.,**it2, 0.);
		Hep3Vector dir1 = (*it1)->direction(poca.flt1());
		Hep3Vector dir2 = (*it2)->direction(poca.flt2());
		double doca = poca.doca() ;
		if(fabs(doca)<fabs(docabest)) {
		  HepPoint pnt1 = (*it1)->position(poca.flt1());
		  HepPoint pnt2 = (*it2)->position(poca.flt2());
		  fitparams->par()(posindex+1) = 0.5*(pnt1.x()+pnt2.x()) ;
		  fitparams->par()(posindex+2) = 0.5*(pnt1.y()+pnt2.y()) ;
		  fitparams->par()(posindex+3) = 0.5*(pnt1.z()+pnt2.z()) ;
		  docabest = doca ;
		  pocastatus = poca.status() ;
		}
	      }
	} else if( mother() && mother()->posIndex()>=0 ) {
	  
	  // let's hope the mother was initialized
	  int posindexmother = mother()->posIndex() ;
	  for(int ipos=1; ipos<=3; ++ipos) {
	    fitparams->par()(posindex+ipos) = 
	      fitparams->par()(posindexmother+ipos) ;
	  }
	} else {
	  // something is wrong!
	  BtaPrintTree treeprinter ;
	  ErrMsg(error) << "There are not sufficient geometric constraints to fit "
			<< "this decay tree. Perhaps you should add a beam constraint. " 
			<< bc()->constraint(BtaConstraint::Beam)
			<< endl 
			<< treeprinter.print(*bc()) << endmsg ;
	  status |= ErrCode::badsetup ;
	}
      }
    }

    // step 3: do the post initialization step of all daughters
    for(daucontainer::const_iterator it = _daughters.begin() ;
	it != _daughters.end() ; ++it) 
      (*it)->initPar2(fitparams) ;
    
    // step 4: initialize the momentum by adding up the daughter 4-vectors
    initMom(fitparams) ;
    
    if(vtxverbose>=3)
      cout << "End of initpar: " 
	   << bc()->pdtEntry()->name() << " ("
	   << fitparams->par()(posindex+1) << ","
	   << fitparams->par()(posindex+2) << ","
	   << fitparams->par()(posindex+3) << ")" << endl ;
    
    return status ;
  }
  
  ErrCode
  InternalParticle::initPar2(FitParams* fitparams)
  {
    // FIX ME: in the unfortunate case (the B-->D0K*- above) that our
    // vertex is still the origin, we copy the mother vertex.
    int posindex = posIndex() ;
    if( hasPosition() && mother() && fitparams->par(posindex+1)==0 && 
	fitparams->par(posindex+2)==0 && fitparams->par(posindex+3)==0 ) {
      int posindexmom = mother()->posIndex() ;
      for(int irow=1; irow<=3; ++irow) 
	fitparams->par(posindex+irow) = fitparams->par(posindexmom+irow) ;
    }
    // step 5: initialize the lifetime
    return initTau(fitparams) ;
  }

  ErrCode
  InternalParticle::initMom( FitParams* fitparams ) const
  {
    int momindex = momIndex() ;
    // reset
    for( int irow=1; irow<=4; ++irow)
      fitparams->par(momindex+irow) = 0 ;

    // now add daughter momenta
    for(daucontainer::const_iterator it = begin() ; it != end() ; ++it) {
      int daumomindex = (*it)->momIndex() ;
      double e2(0) ;
      int maxrow = (*it)->hasEnergy() ? 4 : 3 ;
      for(int irow=1; irow<=maxrow; ++irow) {
	double px = fitparams->par()(daumomindex+irow) ;
	e2 += px*px ;
	fitparams->par(momindex+irow) += px ;
      }
      if(maxrow==3) {
	double mass = (*it)->pdtMass() ;
	fitparams->par(momindex+4) += sqrt(e2+mass*mass) ;
      }
    }
    return ErrCode::success ;
  }
  
  ErrCode
  InternalParticle::initCov(FitParams* fitparams) const
  { 
    ErrCode status ;
    ParticleBase::initCov(fitparams) ;
    for(daucontainer::const_iterator it = _daughters.begin() ;
	it != _daughters.end() ; ++it)
      status |= (*it)->initCov(fitparams) ;
    return status ;
  }
    
  ErrCode
  InternalParticle::projectKineConstraint(const FitParams& fitparams,
					  Projection& p) const
  {
    // these are in fact four independent constraints. i'll filter
    // them as one, making the code simpler at the expense of a bit of
    // CPU.

    // first add the mother
    int momindex = momIndex() ;
    for(int imom=1; imom<=4; ++imom) {
      p.r(imom)               = fitparams.par()(momindex+imom) ;
      p.H(imom,momindex+imom) = 1 ;
    }
    
    // now add the daughters
    for(daucontainer::const_iterator it = _daughters.begin() ;
	it != _daughters.end() ; ++it) {
      int dautauindex = (*it)->tauIndex() ;
      int daumomindex = (*it)->momIndex() ;
      double mass = (*it)->pdtMass() ;
      double e2=mass*mass ;
      int maxrow = (*it)->hasEnergy() ? 4 : 3 ;
      for(int imom=1; imom<=maxrow; ++imom) {
	double px = fitparams.par()(daumomindex+imom) ;
	e2 += px*px ;
	p.r(imom) += -px ;
	p.H(imom,daumomindex+imom) = -1 ;
      }

      if(maxrow==3) {
	// treat the energy for particles that are parameterized with p3
	double energy = sqrt(e2) ;
	p.r(4) += -energy ;
	for(int jmom=1; jmom<=3; ++jmom) {
	  double px = fitparams.par()(daumomindex+jmom) ;
	  p.H(4,daumomindex+jmom) = -px/energy ;
	}
      } else if(dautauindex>=0 && (*it)->charge()!=0) {

	double tau =  fitparams.par()(dautauindex+1) ;
	double lambda = bFieldOverC() * (*it)->charge() ; 
	double px0 = fitparams.par()(daumomindex+1) ;
	double py0 = fitparams.par()(daumomindex+2) ;
	double pt0 = sqrt(px0*px0+py0*py0) ;
	const double posprecision = 1e-4 ; // 1mu
	if( fabs(pt0*lambda*tau*tau) > posprecision ) {
	  double sinlt = sin(lambda*tau) ;
	  double coslt = cos(lambda*tau) ;
	  double px = px0*coslt - py0*sinlt ;
	  double py = py0*coslt + px0*sinlt ;
	  p.r(1) += px0 - px ;
	  p.r(2) += py0 - py ;
	  p.H(1,daumomindex+1) +=  1 - coslt ;
	  p.H(1,daumomindex+2) +=      sinlt ;
	  p.H(1,dautauindex+1) +=  lambda*py ;
	  p.H(2,daumomindex+1) +=     -sinlt ;
	  p.H(2,daumomindex+2) +=  1 - coslt ;
	  p.H(2,dautauindex+1) += -lambda*px ;
	}
      }
    }
    return ErrCode::success ;
  }
  
  ErrCode
  InternalParticle::projectLifeTimeConstraint(const FitParams& fitparams,
					      Projection& p) const
  {
    int tauindex = tauIndex() ;
    assert(tauindex>=0) ;
    double tau = pdtTau() ;
    p.r(1)            = fitparams.par()(tauindex+1) - tau ;
    p.Vfast(1,1)      = tau*tau ;
    p.H(1,tauindex+1) = 1 ;
    return ErrCode::success ;
  }

  ErrCode
  InternalParticle::projectConversionConstraint(const FitParams& fitparams,
						Projection& p) const
  {
    // only works if there are two daughters. constraint those to be parallel:
    // p1.in(p2) - |p1||p2|=0
    assert(_isconversion) ;
    const ParticleBase* dauA = _daughters[0] ;
    const ParticleBase* dauB = _daughters[1] ;
    int daumomindexA = dauA->momIndex() ;
    int daumomindexB = dauB->momIndex() ;
    
    // first calculate the total momenta
    double momA2(0),momB2(0) ;
    for(int irow=1; irow<=3; ++irow) {
      double pxA =  fitparams.par(daumomindexA+irow) ;
      momA2 += pxA*pxA ;
      double pxB =  fitparams.par(daumomindexB+irow) ;
      momB2 += pxB*pxB ;
    }
    double momA(sqrt(momA2)), momB(sqrt(momB2)) ;
    
    // now fill r and H
    p.r(1) = -momA*momB ;
    for(int irow=1; irow<=3; ++irow) {
      double pxA =  fitparams.par(daumomindexA+irow) ;
      double pxB =  fitparams.par(daumomindexB+irow) ;
      p.r(1) += pxA*pxB ;
      p.H(1,daumomindexA+irow) = pxB - pxA/momA * momB ;
      p.H(1,daumomindexB+irow) = pxA - pxB/momB * momA ;
    }

    return ErrCode::success ;
  }
  
  ErrCode
  InternalParticle::projectConstraint(const Constraint::Type type,
				      const FitParams& fitparams, 
				      Projection& p) const 
  {
    ErrCode status ;
    switch(type) {
    case Constraint::mass:
    case Constraint::massEnergy:
      status |= projectMassConstraint(fitparams,p) ;
      //chisq = filterMassConstraintOnDaughters(fitpar) ;
      break ;
    case Constraint::geometric:
      status |= projectGeoConstraint(fitparams,p) ;
      break ;
    case Constraint::kinematic:
      status |= projectKineConstraint(fitparams,p) ;
      break ;
    case Constraint::lifetime:
      status |= projectLifeTimeConstraint(fitparams,p) ;
      break ;
    case Constraint::conversion:
      status |= projectConversionConstraint(fitparams,p) ;
      break ;
    default:
      status |= ParticleBase::projectConstraint(type,fitparams,p) ;
    }
    return status ;
  } 

  void InternalParticle::addToConstraintList(constraintlist& alist, 
					     int depth) const
  {
   // first the daughters
    for(daucontainer::const_iterator it = _daughters.begin() ;
	it != _daughters.end() ; ++it)
      (*it)->addToConstraintList(alist,depth-1) ;

    //double geoprecision  = 1e-5 ; // 1mu
    //double massprecision = 4*pdtMass()*pdtMass()*1e-5 ; // 0.01 MeV

    // the lifetime constraint
    if(tauIndex()>=0 && _lifetimeconstraint) 
      alist.push_back(Constraint(this,Constraint::lifetime,depth,1)) ;
    // the kinematic constraint
    if(momIndex()>=0)
      alist.push_back(Constraint(this,Constraint::kinematic,depth,4)) ;
    // the geometric constraint
    if(mother() && tauIndex()>=0) 
      alist.push_back(Constraint(this,Constraint::geometric,depth,3,0,3)) ;
    // the mass constraint
    if(_massconstraint) {
      if( !_isconversion ) 
	//     if( mother() && mother()->type()==ParticleBase::kUpsilon)
	// 	alist.push_back(Constraint(this,Constraint::massEnergy,depth)) ;
	//       else
	alist.push_back(Constraint(this,Constraint::mass,depth,1,0,10)) ;
      else
	alist.push_back(Constraint(this,Constraint::conversion,depth,1,0,3)) ;
    }
  }

  
  std::string InternalParticle::parname(int thisindex) const
  {
    int id = thisindex ;
    // skip the lifetime parameter if there is no mother
    if(!mother() && id>=3) ++id ;
    return ParticleBase::parname(id) ;
  }

//   struct printfunctor : public unary_function<ParticleBase*,void>
//   {
//     printfunctor(const FitParams* fitpar) : _arg(fitpar)  {}
//     void operator() (const ParticleBase* x) { x->print(_arg) ; }
//     const FitParams* _arg ;
//   };
  
  void InternalParticle::print(const FitParams* fitpar) const
  {
    ParticleBase::print(fitpar) ;
    
//     for_each(_daughters.begin(),_daughters.end(),
// 	     printfunctor(fitpar) ) ;
    
    for(daucontainer::const_iterator it = _daughters.begin() ;
	it != _daughters.end() ; ++it)
      (*it)->print(fitpar) ;
  }

  const ParticleBase* 
  InternalParticle::locate(const BtaCandidate* abc) const
  {
    const ParticleBase* rc = ParticleBase::locate(abc) ;
    for(daucontainer::const_iterator it = _daughters.begin() ;
	!rc && it != _daughters.end(); ++it)
      rc = (*it)->locate(abc) ;
    return rc ;
  }
  
  double InternalParticle::chiSquare(const FitParams* fitparams) const
  {
    double rc = 0;
    for(daucontainer::const_iterator it = _daughters.begin() ;
	it != _daughters.end(); ++it)
      rc += (*it)->chiSquare(fitparams) ;
    return rc ;
  }

  bool InternalParticle::swapMotherDaughter(FitParams* fitparams,
					    const ParticleBase* newmother)
  {
    // routine that switches momentum vectors in a vertex, used for
    // reconstructing the tagging vertex.
    assert((newmother->type()==kBtaComposite||newmother->type()==kBtaResonance)) ;
    daucontainer::iterator it = std::find(_daughters.begin(),_daughters.end(),newmother) ;
    assert( it != _daughters.end() ) ;

    // now start substituting
    // 1. assign the missing particle
    // 2.
    // 3. swap the momenta around
 
    int dummy = newmother->index() ;
    ParticleBase* missingparticle = new MissingParticle(0,this) ;
    missingparticle->updateIndex(dummy) ;

    // invert tau
    if( newmother->tauIndex()>=0 && tauIndex()>=0) {
      double tau = fitparams->par()(newmother->tauIndex()+1) ;
      fitparams->par()(tauIndex()+1) = -tau ;
    }

    // set the btacandidate
    setCandidate( newmother->bc() ) ;

    // do the momentum
    int momindex = momIndex() ;
    int momindexmother = newmother->momIndex() ;
    int momindexmissing = missingparticle->momIndex() ;

    int maxrow = newmother->hasEnergy() && hasEnergy() ? 4 : 3 ;
    double energy2(0) ;
    for( int row=1; row<=maxrow; ++row) { 
      double pxin  = fitparams->par()(momindexmother+row) ;
      double pxout = fitparams->par()(momindex      +row) ;
      // the new missing particle
      fitparams->par()(momindexmissing+row) = 2*pxin - pxout ;
      fitparams->par()(momindex+row) = pxin ;
      energy2 += pxin*pxin ;
    }

    if( newmother->hasEnergy() && hasEnergy() ) {
      double mass = newmother->pdtMass() ;
      double Ein  = sqrt(energy2+mass*mass) ;
      double Eout = fitparams->par()(momindex+4) ;
      fitparams->par()(momindexmissing+4) = 2*Ein - Eout ;
      fitparams->par()(momindex+4)        = Ein ;
    }
    
    ParticleBase* newmothercopy = *it ;
    *it = missingparticle ;
    delete newmothercopy ;
     
    return true ;
  }

  

  void InternalParticle::forceP4Sum(FitParams& fitparams) const 
  {
    // because things are not entirely linear, p4 is not exactly
    // conserved at the end of fits that include mass
    // constraints. this routine is called after the tree is fitted to
    // ensure that p4 'looks' conserved.
   
    // first the daughters
    for(daucontainer::const_iterator it = _daughters.begin() ;
	it != _daughters.end(); ++it) (*it)->forceP4Sum(fitparams) ;
    
    // now yourself (this is a bit expensive, but easy)
    int momindex = momIndex() ;
    if(momindex>0) {
      Projection p(fitparams.dim(),4) ;
      projectKineConstraint(fitparams,p) ;
      for(int imom=1; imom<=4; ++imom) fitparams.par()(momindex+imom) -= p.r(imom) ;
    }
  }
}
