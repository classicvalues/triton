#ifndef _ISAAC_SCHEDULER_EXECUTE_H
#define _ISAAC_SCHEDULER_EXECUTE_H

#include "isaac/database/database.h"
#include "isaac/symbolic/expression.h"

namespace isaac
{

/** @brief Executes a array_expression on the given queue for the given models map*/
void execute(controller<array_expression> const & , database::map_type &);

}

#endif
