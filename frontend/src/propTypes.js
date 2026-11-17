import PropTypes from 'prop-types'

export const toastType = PropTypes.shape({
  ok: PropTypes.func.isRequired,
  bad: PropTypes.func.isRequired,
  warn: PropTypes.func.isRequired,
})

export const datasourceType = PropTypes.shape({
  id: PropTypes.string.isRequired,
  // the Database tab reads this to look up which businesses curate the connection
  tenant_id: PropTypes.string,
  name: PropTypes.string,
  type: PropTypes.string,
  enabled: PropTypes.bool,
  mdl: PropTypes.shape({
    tables: PropTypes.objectOf(PropTypes.arrayOf(PropTypes.string)),
    allowed_tables: PropTypes.arrayOf(PropTypes.string),
    pii_columns: PropTypes.arrayOf(PropTypes.string),
    descriptions: PropTypes.object,
    metrics: PropTypes.array,
    business_concepts: PropTypes.array,
    routing_aliases: PropTypes.arrayOf(PropTypes.string),
    reference_values: PropTypes.object,
    examples: PropTypes.array,
  }),
})

export const tenantType = PropTypes.shape({
  id: PropTypes.string.isRequired,
  name: PropTypes.string.isRequired,
  descr: PropTypes.string,
  icon: PropTypes.string,
  status: PropTypes.string,
  owner_email: PropTypes.string,
})
