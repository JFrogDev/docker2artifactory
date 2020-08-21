from DockerEEHTTPAccess import DockerEEHTTPAccess
import urllib
import logging
'''
    Simple API for accessing UCP resources

    Supports:


'''
class UCPAccess(DockerEEHTTPAccess):
    def __init__(self, url, username=None, password=None, ignore_cert=False, exlog=False):
        super(UCPAccess, self).__init__(url, username, password, ignore_cert, exlog)
        self.log = logging.getLogger(__name__)
        self.artifactory_users = []

    '''
        Test connection with UCP
    '''
    def test_connection(self):
        return bool(super(UCPAccess, self).get_call_wrapper('/id/'))

    '''
        Gets the list of all organizations
        @return None if there was an error, else the a list of available organizations
    '''
    def get_organizations(self):
        return super(UCPAccess, self).get_with_pagination('accounts/', 'accounts', 'name', self.__get_organizations_page_handler)

    def __get_organizations_page_handler(self, result, page_results):
        for account in page_results:
            if account['isOrg'] == True:
                result.append(account['name'])

    '''
        Gets the list of all users
        @return None if there was an error, else the a list of available team of a given organization
    '''
    def get_users(self, artifactory_users):
        self.artifactory_users = artifactory_users
        users = []
        for user in artifactory_users:
            if self.userExists(user):
                self.log.info("Found '%s' user in UCP and Artifactory" % user)
                users.append(user)
        return users

    '''
        Get the list of all teams of a given organizations
        @return None if there was an error, else the a list of available team of a given organization
    '''
    def get_teams(self, organization):
        org_encoded = urllib.quote(organization.encode('utf8'))
        return super(UCPAccess, self).get_with_pagination("accounts/" + org_encoded + "/teams/", 'teams', 'name', self.__get_teams_page_handler)

    def __get_teams_page_handler(self, result, page_results):
        for team in page_results:
            result.append(team['name'])

    '''
        Get the list of members of a given team
        @return None if there was an error, else the a list of available members of a given team
    '''
    def get_members(self, organization, team):
        org_encoded = urllib.quote(organization.encode('utf8'))
        team_encoded = urllib.quote(team.encode('utf8'))
        return super(UCPAccess, self).get_with_pagination("accounts/" + org_encoded + "/teams/" + team_encoded + "/members/", 'members', 'member.id', self.__get_members_page_handler)

    def __get_members_page_handler(self, result, page_results):
        for member in page_results:
            result.append(member['member']['name'])
